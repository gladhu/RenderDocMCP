"""MCP 工具参数容错。

背景: LLM 调用 MCP 工具时, 偶尔会把整型参数传成空字符串 '' (占位符) 或
'5' 这样的数字字符串, shader stage 也常写成 vs/ps/cs 缩写。FastMCP/pydantic
对 '' 直接报 int_parsing, 整次调用在到达工具函数体之前就被拒绝, 而 LLM 重试
时往往还是发同样的值, 形成死循环。

做法: 在参数类型上挂 pydantic BeforeValidator, 在校验阶段把脏值洗干净, 工具
函数体与 JSON schema 都不用改 (schema 里仍是 integer / 枚举, 客户端看到的
契约不变):
  - 可选/带默认值的整型: '' / null → 回落到函数签名里的默认值
  - 必填整型: '' / null → 抛出带示例的明确错误, 而不是 int_parsing
  - '5' / ' 5 ' / 5.0 → 5
  - 整型列表: [1,2] / '1,2,3' / '[1,2]' / 单个标量 都接受
  - stage: 接受全名与 vs/ps/cs/gs/hs/ds/frag/comp 等缩写, 大小写不敏感

_as_int / _as_int_list / _as_stage 的解析规则参考 ivyzyshao/RenderDocMCP
mcp_server/params.py。
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator
from pydantic_core import PydanticUseDefault


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _as_int(value: Any, name: str = "参数") -> int:
    """把 LLM 传来的脏整型解析成 int, 无法解析时抛出带示例的 ValueError。"""
    if _is_blank(value):
        raise ValueError(
            "%s 需要一个整数 (例如 5), 收到空值。请传整数, 不要传空字符串或 null。" % name
        )
    if isinstance(value, bool):
        raise ValueError("%s 需要整数, 收到 bool: %r" % (name, value))
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                raise ValueError("%s 需要一个整数 (例如 5), 无法解析收到的值: %r" % (name, value))
    raise ValueError("%s 需要一个整数, 收到: %r" % (name, value))


def _as_int_list(value: Any, name: str = "参数") -> list[int]:
    """整型列表: list / '1,2,3' / '[1,2,3]' / 单个标量 → list[int]。"""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                raise ValueError("%s 需要整数列表 (例如 [1, 2, 3]), 收到: %r" % (name, value))
        else:
            value = [p for p in (s.strip() for s in text.split(",")) if p]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("%s 需要整数列表 (例如 [1, 2, 3]) 或 null, 收到: %r" % (name, value))
    return [_as_int(v, name) for v in value]


# shader stage 缩写 → RenderDoc 全名。
_STAGE_ALIASES = {
    "vs": "vertex", "vert": "vertex",
    "hs": "hull",
    "ds": "domain",
    "gs": "geometry",
    "ps": "pixel", "frag": "pixel", "fragment": "pixel", "fs": "pixel",
    "cs": "compute", "comp": "compute",
}
_STAGES = ("vertex", "hull", "domain", "geometry", "pixel", "compute")


def _as_stage(value: Any) -> str:
    """接受全名与缩写, 返回 extension 认识的全名。"""
    if _is_blank(value):
        raise ValueError("stage 需要 shader stage 名 (如 pixel / ps), 收到空值。")
    text = str(value).strip().lower()
    text = _STAGE_ALIASES.get(text, text)
    if text not in _STAGES:
        raise ValueError(
            "stage 不是有效的 shader stage: %r (支持全名 vertex/pixel/compute 或缩写 vs/ps/cs 等)"
            % (value,)
        )
    return text


# ---- BeforeValidator 实现 ------------------------------------------------

def _required_int(value: Any) -> int:
    return _as_int(value)


def _defaulted_int(value: Any) -> int:
    if _is_blank(value):
        raise PydanticUseDefault()
    return _as_int(value)


def _defaulted_int_list(value: Any) -> list[int]:
    if _is_blank(value):
        raise PydanticUseDefault()
    values = _as_int_list(value)
    if not values:
        # an empty selection would otherwise mean "no filter" and replay every draw
        raise ValueError("空列表没有意义：省略该参数表示全部，否则至少给一个整数")
    return values


def _required_stage(value: Any) -> str:
    return _as_stage(value)


def _defaulted_stage(value: Any) -> str:
    if _is_blank(value):
        raise PydanticUseDefault()
    return _as_stage(value)


# ---- 给工具签名用的类型别名 -----------------------------------------------

# 必填整型 (event_id / pass_index / x / y ...): 空值给出明确错误
ReqInt = Annotated[int, BeforeValidator(_required_int)]
# 带默认值的整型: 空值回落到签名里的默认值
LInt = Annotated[int, BeforeValidator(_defaulted_int)]
OptInt = Annotated[int | None, BeforeValidator(_defaulted_int)]
OptIntList = Annotated[list[int] | None, BeforeValidator(_defaulted_int_list)]

# 必填 stage (六个阶段)
StageReq = Annotated[
    Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"],
    BeforeValidator(_required_stage),
]
# 可选 stage (六个阶段, 默认 None)
StageOpt = Annotated[
    Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"] | None,
    BeforeValidator(_defaulted_stage),
]
# 只支持 vertex / pixel 的工具 (有默认值)
StageVP = Annotated[Literal["vertex", "pixel"], BeforeValidator(_defaulted_stage)]
# vertex / pixel / compute, 默认 None
StageVPCOpt = Annotated[
    Literal["vertex", "pixel", "compute"] | None, BeforeValidator(_defaulted_stage)
]
