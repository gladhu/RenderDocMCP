# RenderDoc MCP Server

作为 RenderDoc UI 扩展运行的 MCP 服务器。AI 助手通过它读取 RenderDoc 抓帧数据，用于图形调试（D3D11/12、GL/GLES、Vulkan）和移动端性能分析。

## 架构

**进程分离**：

```
Claude/AI Client (stdio)
        │
        ▼
MCP Server Process (普通 Python + FastMCP 2.0)
        │ 基于文件的 IPC (%TEMP%/renderdoc_mcp/)
        ▼
RenderDoc Process (扩展 + 文件轮询)
```

## 用哪个 RenderDoc

- **用官方构建**（`C:\Program Files\RenderDoc`，1.46，内置 Python 3.6 + PySide2）。
- **定制构建 1.43**（`D:\Program Files\RenderDoc_AI_20260716`，内嵌 Python 3.14，无 PySide2）的 Python 胶水层有堆损坏：
  从扩展调用 `ctx.Replay().BlockInvoke(...)`，哪怕回调是空函数，几次后就会在不相干的位置（GC、`re`、import）
  访问冲突。实测：同一个"空回调 + GC"压力测试，官方 1.46 连续 10 轮正常，定制 1.43 第 1–2 轮必崩（真机回放和
  本机回放都一样）；同一份 `renderdoc.pyd` 脱离 GUI 独立回放不崩。转储里能看到被释放两次的 1 元组和低 32 位被减 1
  的指针（无 GIL 的非原子 `dec`）。这是构建本身的问题，扩展侧绕不开。
- 以前记录的"回放线程读 X 会崩"（`GetAPIProperties`、递归遍历后读 `action.flags`、`tex.creationFlags`、
  持有 `GetVBuffers()` 返回值等）都是这个堆损坏在不同位置的表现，不是这些调用本身的问题。
- 官方构建远程回放需要手机上装同版本服务端 APK（连接时自动安装）。Android 11+ 首次启动要允许"所有文件访问"：
  `adb shell appops set --uid org.renderdoc.renderdoccmd.arm64 MANAGE_EXTERNAL_STORAGE allow`。两个版本包名相同，会互相覆盖。
- `scripts/capture_android.py` 抓帧用定制构建目录里的层库和 `renderdoc.pyd`（独立 Python，不受上面的问题影响）；
  1.43 抓的帧可以用 1.46 打开。

## 项目结构

```
RenderDocMCP/
├── mcp_server/                        # MCP 服务器
│   ├── server.py                      # FastMCP 入口，工具定义
│   ├── bridge/client.py               # 文件 IPC 客户端
│   ├── compare.py / report.py         # 快照、对比、报告
│   ├── draw_analysis.py / texture_analysis.py
│   ├── malioc.py / aoc.py             # 离线着色器编译器
│   ├── params.py                      # 参数容错
│   └── result_cache.py                # 大结果缓存 + 分页
├── renderdoc_extension/               # RenderDoc 扩展（Python 3.6 兼容，只用标准库）
│   ├── __init__.py                    # register()/unregister()
│   ├── socket_server.py               # 文件 IPC 服务端
│   ├── request_handler.py             # 请求分发
│   ├── renderdoc_facade.py            # 门面，BlockInvoke 到回放线程
│   ├── services/                      # 各类分析（action / pass / bandwidth / gl_state / vk_passes ...）
│   └── utils/
├── scripts/
│   ├── install_extension.py           # 安装扩展
│   ├── capture_android.py             # Android Vulkan 抓帧
│   └── e2e_check.py                   # 端到端检查（需已打开抓帧）
└── tests/                             # 离线单元测试（unittest）
```

## MCP 工具

| 工具 | 说明 |
|------|------|
| `list_captures` | 列出目录内的 .rdc |
| `open_capture` | 打开抓帧（自动关闭当前抓帧），等加载完成并核对加载的是请求的文件 |
| `get_capture_status` | 抓帧加载状态 |
| `get_draw_calls` | DC 列表（层级、过滤） |
| `get_frame_summary` | 整帧统计（DC 数、marker 等） |
| `find_draws_by_shader` / `find_draws_by_texture` / `find_draws_by_resource` | 按 shader 名 / 贴图名 / 资源 ID 反查 DC |
| `get_draw_call_details` | 单个 DC 详情 |
| `get_action_timings` | GPU 计时（移动端 tile GPU 上逐 DC 计时偏大，用着色成本工具） |
| `get_shader_info` | shader 源码 / 常量缓冲 |
| `get_buffer_contents` | buffer 数据（可指定偏移/长度） |
| `get_texture_info` / `get_texture_data` | 贴图元数据 / 像素（mip/slice/3D 切片） |
| `get_pipeline_state` | 完整管线状态 |
| `read_constant_buffer` | 读一个常量缓冲。slot 是反射里的块 index 或 cbuffer 名（不是 API 绑定点）；省略 slot 列出全部 |
| `list_buffers` | buffer 清单（大小/名字过滤、group_by_size/name、unused_only、with_usage） |
| `get_buffer_bindings_at_event` | 一个 DC 绑定的 VB/IB/常量缓冲 |
| `compare_pipeline_states` | 比较两个事件的管线状态，只返回不同字段（查没合批的原因） |
| `get_api_calls` | 原始 API 调用及参数（event_id 或 start/end_event，name_filter） |
| `find_pixels` | 按通道条件找像素（网格采样，带包围盒） |
| `get_cached_message` | 超大结果自动缓存后的分页/搜索；列表按条目分页，超大的非列表字段（如 shader 源码）按行分页；打开新抓帧时清空 |
| （实现说明）`analyze_state_changes` / `diagnose_mobile_risks` | GL 下不逐 DC 回放：`services/gl_stream.py` 读调用流，`services/gl_state.py` 跟踪 glEnable/glBlendFunc/glUseProgram 等还原每个 DC 的状态；discard 检测按 program 缓存。839 DC 的帧 118s→0.2s、115s→13s，结果与回放路径一致。非 GL 仍走回放 |
| `analyze_pass_bandwidth` | 按 FBO 切 tile pass，估算附件 Load/Store 带宽并给出 wasted_store / load_without_clear / render_target_reopened / flush_by_readback / msaa_stored 等问题（GLES 按调用流推断，Vulkan 读 render pass 的 loadOp/storeOp）。issues 最多 max(50, top_n) 条，issue_count / issues_by_type / avoidable_mb 覆盖全部 |
| `estimate_texture_bandwidth` | 贴图采样足迹（`mcp_server/texture_analysis.py`，数据来自 `get_draw_coverage(with_textures=True)` + `list_frame_textures`）；上限估算，每像素最多取一个 texel，不含缓存/各向异性 |
| `estimate_geometry_cost` / `analyze_overdraw` | 顶点预算与过度绘制（`mcp_server/draw_analysis.py`，数据来自 `get_draw_coverage(with_state=True)` + malioc 顶点/像素周期）。覆盖像素/三角形数含背面剔除，阈值已补偿 |
| `frame_performance_report` | 基于快照生成记分卡 + 排序后的优化项（`mcp_server/report.py`）。每类带宽问题保留最重的 3 条，其余合并成一条。百分比只在各自领域内可比，阈值是经验值 |
| `snapshot_metrics` / `diff_snapshots` / `compare_captures` | 指标快照与前后对比。shader 用源码 hash 匹配，着色成本差拆成 shader_cost_delta 与 coverage_delta；compare 每取完一个快照立即保存 |
| `stat_instancing_efficiency` | 整帧 instancing 统计（按 pass，无效 instance DC 数） |

### get_draw_calls 过滤参数

```python
get_draw_calls(
    include_children=True,      # 包含子 action
    marker_filter="Camera.Render",  # 只取该 marker 下
    exclude_markers=["GUI.Repaint", "UIR.DrawChain"],  # 排除的 marker
    event_id_min=7372,          # event_id 范围起点
    event_id_max=7600,          # event_id 范围终点
    only_actions=True,          # 去掉 marker（只要 DC）
    flags_filter=["Drawcall", "Dispatch"],  # 只要这些标志
)
```

### 抓帧管理

```python
list_captures(directory="D:\\captures")
# → {"count": 3, "captures": [{"filename": "game.rdc", "path": "...", "size_bytes": 12345, "modified_time": "..."}, ...]}

open_capture(capture_path="D:\\captures\\game.rdc")            # 本机
open_capture(capture_path="D:\\captures\\phone.rdc", use_remote=True)   # 真机回放
# → {"success": true, "filename": "game.rdc", "api": "D3D11"}
```

### 反查

```python
find_draws_by_shader(shader_name="Toon", stage="pixel")      # 部分匹配
find_draws_by_texture(texture_name="CharacterSkin")           # 部分匹配
find_draws_by_resource(resource_id="ResourceId::12345")       # 完全匹配
```

### GPU 计时

```python
get_action_timings()
get_action_timings(event_ids=[100, 200, 300])
get_action_timings(marker_filter="Camera.Render", exclude_markers=["GUI.Repaint"])
```

计时计数器取决于硬件/驱动，返回 `available: false` 时该抓帧没有计时信息。

## Vulkan 注意事项

- Vulkan 的绘制动作 `outputs/depthOut` 为空，目标属于 render pass：用 `services/draw_targets.py` / `vk_passes.py`
  由调用流得到事件到目标的映射，不要依赖 `action.outputs`。
- 交换链图像按资源名识别（`Swapchain Image N` / `Backbuffer ...`）。
- Shader 是 SPIR-V（`refl.rawBytes`），没有 GLSL；discard 用 SPIR-V 指令扫描，成本交给 malioc 的 `.spv` 输入。
- 原型开发可以不经 GUI：adb forward 手机远程服务 `localabstract:renderdoc_39920`，用 `renderdoc` 的
  `CreateRemoteServerConnection` + `OpenCapture` 在独立 Python 里回放，更快。
- 抓帧用 `scripts/capture_android.py`（debuggable 应用，`run-as` 放入层库，不必降 targetSdk）。

## 通信协议

基于文件的 IPC：
- IPC 目录：`%TEMP%/renderdoc_mcp/`，可用 `RENDERDOC_MCP_IPC_DIR` 改（多个 qrenderdoc 实例时各用各的；两端要设成同一个值）
- 每个请求：客户端写 `request_<id>.json.tmp` 再改名为 `request_<id>.json`；扩展处理后同样先写 `.tmp` 再改名为
  `response_<id>.json`。多个 MCP 服务器进程可以共用一个 RenderDoc。客户端超时会撤回尚未被取走的请求
- 旧协议（`request.json` + `lock` + `response.json`）仍然支持，供未重启的旧客户端使用
- 轮询间隔：100ms（RenderDoc 侧）
- 崩溃时 faulthandler 写到 IPC 目录下的 `fault.log`

## 开发说明

- RenderDoc 内置 Python 没有 socket/QtNetwork 模块，所以用文件 IPC
- 扩展只用 Python 3.6 标准库语法（官方构建是 3.6）
- 访问 ReplayController 通过 `BlockInvoke`。有 PySide2 时 QTimer 在 UI 线程轮询；没有 PySide2 时由工作线程轮询，
  再用 `MiniQtHelper.InvokeOntoUIThread` 交给 UI 线程（传入的回调是创建一次后一直持有的对象：qrenderdoc 的回调胶水
  在不持有 GIL 时修改它的引用计数）
- 枚举名用整数表（`utils/helpers.py` 的 `_ENUM_TABLES`），不依赖各构建 SWIG 枚举的反射方式
- 更新扩展后要重启 RenderDoc（qrenderdoc 会一直持有已加载的模块）
- 测试：`python -m unittest discover -s tests`（离线）；`scripts/e2e_check.py gles|vulkan`（需已打开对应抓帧）

## 参考链接

- [FastMCP](https://github.com/jlowin/fastmcp)
- [RenderDoc Python API](https://renderdoc.org/docs/python_api/index.html)
- [RenderDoc Extension Registration](https://renderdoc.org/docs/how/how_python_extension.html)
