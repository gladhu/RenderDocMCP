"""超大工具结果的缓存与分页取回。

背景: get_draw_calls / list_buffers / get_pipeline_state 等在大抓帧上会返回几十万字符,
超过 MCP 客户端能接受的单条输出上限(或把 LLM 上下文占满)。这里在 FastMCP 的
middleware 里拦截工具结果: 超过阈值就把完整结果存在本进程内存里, 只返回一个带
message_id、各集合大小和前几项预览的"信封", LLM 再用 get_cached_message 分页或搜索。

设计与实现思路参考 renderdoc-mcp-brainfkli 的 mcpserver/src/renderdoc_mcp/runtime.py
(MessageCache / prepare_tool_result / query_cached_message)。差异: 用 fastmcp 2.x 的
Middleware 接入而不是替换 add_tool; 阈值默认更大(该仓库 4096 字符对我们太小);
不做它的"单线程串行 + TOOL_BUSY"(fastmcp 同步工具本来就在事件循环里串行执行)
和空闲自动关闭(我们没有 close_capture)。
"""

import json
import os
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

MAX_DIRECT_CHARS = max(2000, int(os.environ.get("RENDERDOC_MCP_MAX_RESPONSE_CHARS", "20000")))
CACHE_MAX_ENTRIES = max(8, int(os.environ.get("RENDERDOC_MCP_CACHE_MAX_ENTRIES", "64")))
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
PREVIEW_ITEMS = 3
PREVIEW_ITEM_CHARS = 400

# 返回的是整块二进制(Base64), 翻页没有意义, 保持原样。
EXEMPT_TOOLS = frozenset({"get_cached_message", "get_texture_data", "get_buffer_contents"})


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _lists_of(payload: Any) -> dict[str, list]:
    """顶层字典里的列表字段(可翻页的视图)。"""
    if isinstance(payload, list):
        return {"(root)": payload}
    if isinstance(payload, dict):
        return {k: v for k, v in payload.items() if isinstance(v, list)}
    return {}


class MessageCache:
    """进程内的 LRU 缓存。"""

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES):
        self.max_entries = max_entries
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, tool_name: str, payload: Any, chars: int) -> dict:
        entry = {
            "message_id": "msg_" + uuid.uuid4().hex[:12],
            "tool_name": tool_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "chars": chars,
            "payload": payload,
        }
        with self._lock:
            self._entries[entry["message_id"]] = entry
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return entry

    def get(self, message_id: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(message_id)
            if entry is not None:
                self._entries.move_to_end(message_id)
            return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = MessageCache()


def _views(payload: Any) -> list[dict]:
    return [{"key": k, "type": "list", "count": len(v)} for k, v in _lists_of(payload).items()]


def _shrink(item: Any) -> Any:
    text = _dumps(item)
    return item if len(text) <= PREVIEW_ITEM_CHARS else text[:PREVIEW_ITEM_CHARS] + "…"


def _preview(payload: Any) -> Any:
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, list):
                out[key] = {"count": len(value), "first": [_shrink(v) for v in value[:PREVIEW_ITEMS]]}
            elif len(_dumps(value)) <= PREVIEW_ITEM_CHARS:
                out[key] = value
            else:
                out[key] = "<%s, %d chars>" % (type(value).__name__, len(_dumps(value)))
        return out
    if isinstance(payload, list):
        return {"count": len(payload), "first": [_shrink(v) for v in payload[:PREVIEW_ITEMS]]}
    return _shrink(payload)


def store_if_large(tool_name: str, payload: Any) -> dict | None:
    """结果超过阈值时存入缓存并返回信封, 否则返回 None。"""
    chars = len(_dumps(payload))
    if chars <= MAX_DIRECT_CHARS:
        return None
    entry = _cache.put(tool_name, payload, chars)
    return {
        "status": "cached",
        "message": "Result too large for direct output (%d chars > %d); the full result is "
                   "cached on the MCP server. Page through it with get_cached_message." % (chars, MAX_DIRECT_CHARS),
        "message_id": entry["message_id"],
        "tool_name": tool_name,
        "approx_chars": chars,
        "available_views": _views(payload),
        "preview": _preview(payload),
        "next_step": {"tool": "get_cached_message",
                      "arguments": {"message_id": entry["message_id"], "page": 1, "page_size": DEFAULT_PAGE_SIZE}},
    }


def query_cached_message(message_id: str, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
                         collection: str | None = None, search: str | None = None) -> dict:
    """翻页或搜索一条被缓存的大结果。"""
    entry = _cache.get(message_id)
    if entry is None:
        raise ValueError("Cached message %r not found (it may have been evicted; re-run the original tool)"
                         % message_id)
    payload = entry["payload"]
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    lists = _lists_of(payload)

    result: dict[str, Any] = {
        "message_id": message_id,
        "tool_name": entry["tool_name"],
        "approx_chars": entry["chars"],
        "available_views": _views(payload),
    }
    if collection is not None and collection not in lists:
        raise ValueError("Unknown collection %r; available: %s" % (collection, sorted(lists) or "none"))
    if lists:
        key = collection or max(lists, key=lambda k: len(lists[k]))
        items = lists[key]
        if search:
            needle = search.lower()
            items = [it for it in items if needle in _dumps(it).lower()]
            result["search"] = {"query": search, "matches": len(items)}
        total = len(items)
        pages = max(1, -(-total // page_size))
        start = (page - 1) * page_size
        result.update({
            "collection": key,
            "page": page,
            "page_size": page_size,
            "total_items": total,
            "total_pages": pages,
            "items": items[start:start + page_size],
        })
        if isinstance(payload, dict):
            result["other_fields"] = {k: v for k, v in payload.items()
                                      if k not in lists and len(_dumps(v)) <= PREVIEW_ITEM_CHARS}
        if page < pages:
            result["next_page"] = page + 1
        return result

    # 没有列表字段: 把格式化后的 JSON 按行翻页
    lines = json.dumps(payload, ensure_ascii=False, indent=1, default=str).splitlines()
    if search:
        needle = search.lower()
        lines = [ln for ln in lines if needle in ln.lower()]
        result["search"] = {"query": search, "matches": len(lines)}
    total = len(lines)
    pages = max(1, -(-total // (page_size * 4)))
    size = page_size * 4
    start = (page - 1) * size
    result.update({"collection": "(lines)", "page": page, "total_lines": total, "total_pages": pages,
                   "lines": lines[start:start + size]})
    if page < pages:
        result["next_page"] = page + 1
    return result


def clear() -> None:
    _cache.clear()


class ResultCacheMiddleware(Middleware):
    """超过阈值的工具结果换成缓存信封。"""

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        name = getattr(context.message, "name", "")
        if name in EXEMPT_TOOLS:
            return result
        payload = result.structured_content
        if payload is None:
            texts = [c.text for c in (result.content or []) if isinstance(c, TextContent)]
            if not texts:
                return result
            try:
                payload = json.loads("\n".join(texts))
            except ValueError:
                payload = "\n".join(texts)
        # fastmcp 把非对象返回值包成 {"result": ...}
        if isinstance(payload, dict) and set(payload) == {"result"} and not isinstance(payload["result"], dict):
            payload = payload["result"]
        envelope = store_if_large(name, payload)
        if envelope is None:
            return result
        return ToolResult(content=[TextContent(type="text", text=_dumps(envelope))],
                          structured_content=envelope)
