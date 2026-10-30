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
│   ├── imagediff.py                   # PNG 解码/编码 + 图像差异（diff_textures）
│   ├── vk_sync.py                     # Vulkan barrier/layout 解码与规则（analyze_vulkan_sync）
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
│   ├── dev_cycle.py                   # 重启 RenderDoc、部署扩展、连手机、开抓帧
│   ├── e2e_check.py                   # 端到端检查（需已打开抓帧）
│   └── e2e_tools.py                   # 调试/优化工具的真机端到端检查（经 MCP 层，要求在手机上回放）
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
| `get_debug_messages` | 抓帧里的 API 验证/驱动调试消息（Vulkan 验证层、KHR_debug、D3D 调试层）和回放期警告；按 (id, 文本) 归并计数，可按严重度/类别/来源/文本/事件范围过滤。数据来自 UI 侧 `ctx.DebugMessages()`，不占回放线程；抓帧时没开验证层则为空，不代表帧合法 |
| `render_overlay` | 在一个 DC 上渲染 RenderDoc 调试 overlay（在手机上回放时由手机 GPU 渲染），存成 PNG 并给出数字：depth / stencil（通过 vs 失败像素）、backface_cull、drawcall、wireframe、viewport_scissor（带视口/裁剪矩形）、nan、clipping、quad_overdraw_draw/pass（逐像素 overdraw 计数：均值/最大/直方图）、triangle_size_draw/pass（按三角形面积分桶的像素数）。`services/targets.py` 统一解析 "event 的 color0/depth 是哪张图"（Vulkan 走 render pass 模型）。实测：depth 的通过/失败像素与 `get_draw_coverage` 逐像素一致；quad overdraw 在 GL/GLES 上全 0，结果里带 `supported: false` 和提示；`clear_before_*` 的纹理无数据，不提供 |
| `get_gpu_counters` | 回放设备（手机）的硬件计数器，按 DC 取值。先不带参数列出可用集合：Mali-G610 上 **GLES 抓帧有 GPU Duration + 37 个 Arm HWCPipe 计数器**（外部内存读写字节、shader 各类周期、Early/LateZ、Tiles、Pixels…），**Vulkan 抓帧只有 GPU Duration 和 Samples Passed**（无 Arm 计数器）。选择器：短名 / 计数器名 / id / `@arm` `@generic` `@all`。Arm 计数器是周期采样后摊到各事件上的，逐 DC 值是小数估计，只用于排序和前后对比。GPU 时间随手机 GPU 频率变化（同一批 DC 重复测量 27.7–29.5 ms，`get_action_timings` 在 28 ms 与 20–24 ms 间跳变），所以对比要用 `repeat`（3–5 次取中位数，返回 run_totals 和 run_spread_pct）。Vulkan 的 Samples Passed 与 depth overlay 覆盖像素逐像素一致 |
| `diff_textures` | 两张图的像素差异：数量/比例、最大差、各通道最大差、rmse/psnr、包围盒、差异最集中的网格区域，并写热力图 PNG。每一侧可以是 PNG 文件、某事件的渲染目标（event_id+target）或某资源（可带 event_id）；B 侧没写的字段继承 A（`event_id_a=100, event_id_b=200` = 这两个事件之间目标变了什么）。跨抓帧对比：先在各抓帧里 `save_texture`，再用 `path_a/path_b` 比。逻辑在 `mcp_server/imagediff.py`（只依赖 zlib 的 PNG 解码/编码，2322×1080 解码约 1 s）。按 8-bit PNG 比较，HDR 目标会被导出截断。实测不变量：DC 前后同一目标的差异像素数 ≤ 该 DC 通过深度测试的像素数 |
| `replace_shader` / `revert_shader` | 改 shader、在回放设备（手机）上重新编译并替换，用新 shader 重放；之后所有工具（计数器、overlay、diff、像素读取）看到的都是改后的 shader，直到 `revert_shader` 或换抓帧。GLES：对 GLSL 做 `edits`（find/replace）或整体 `source`；Vulkan 只有 SPIR-V，用 `source_path`（.spv 二进制或 base64 文本）或 `source`，GLSL 需 `encoding="glsl"`。`get_shader_source(output_path=...)` 可把原 SPIR-V 导出成 base64 文件（太大，直接返回会被结果缓存分页）。**替换作用于 shader，不是单个 DC**：Vulkan event 203 的像素 shader 被 17 个 DC 共用，改常量后差异 41170 像素 > 该 DC 覆盖的 23092。设备编译器的错误原样返回（`compiled:false`，原 shader 不受影响）。**Vulkan 的 BuildTargetShader 对任何字节都返回成功**：截断的 SPIR-V 被“替换”后回放的读回全部失败（revert 也不能完全恢复，要重开抓帧/重启），所以送设备前先做结构校验（魔数、字对齐、指令长度链、id bound）。实测闭环：GLES event 824 输出改成洋红，差异像素 207826 = 该 DC 覆盖像素；还原后差异 0；无改动重建与原图像素级相同。验证优化时**不要只看单个 Arm 计数器**：同一 shader 还原后 ShaderCycles 离散度可达 174%（repeat=5），GPU Duration 约 5–8%；常量输出 shader 的 ShaderCycles 降 90% 但 GPU Duration 只降 17%（该 DC 受带宽而非 ALU 限制）。discard 不影响 depth overlay / get_draw_coverage，要用 pixel_history 或 diff_textures 验证 |
| `debug_shader` | 单步调试一次 shader 调用（pixel：x,y；vertex：vertex/instance/index；compute：group/thread），用该事件真实的输入、常量和贴图，在手机上也能跑（RenderDoc 模拟执行）。**只支持 Vulkan 和 D3D，GL/GLES 会明确拒绝**（改用 `replace_shader` 把值输出成颜色再读像素）。返回：总步数、`first_nan_or_inf`（第一个产生 NaN/Inf 的步骤、变量、值）、`nan_at_end`（最终值仍是 NaN/Inf 的变量）、该次调用的输入、`final_values`（每个变量最后一次写入）、前 `max_steps` 步（只含该步改变的变量）；`only_changes_of=["_651"]` 只跟踪指定变量（子串匹配）。无调试信息的 shader 变量名是 SPIR-V id（`_582`…），与 `get_shader_info(...).disassembly` 里的 id 一致，那就是图例（`Output float4* _582 : Location(0)` 是颜色输出）。实测：Vulkan event 203 的一个像素 641 步 0.8 s；轨迹最终颜色 `[0.18167, 0.48552, 0.10077]` 对帧里该像素 `[0.1816, 0.4844, 0.1001]`（手机 GPU 实际输出），模拟结果与真实输出一致。**调试器运行的是被 `replace_shader` 替换后的 shader**：把一个常量改成 NaN 后，`first_nan_or_inf` 精确指到 `_267 * 0.7000` 那条指令；但这个 NaN 被后面的 `FMax/FClamp` 吸收，帧里的像素没变——所以“出现过 NaN”和“NaN 到了输出”要分开看，用 `nan_at_end`。调试器用 0xCCCCCCCC 标记未初始化的变量，结果里显示为 `"uninit"`，step 0 里声明变量的噪声折叠成 `uninitialised_declared` 计数；没有调试信息时 `line` 为 null。像素没被该 DC 写过时报错 |
| `analyze_vulkan_sync` | Vulkan 的 barrier 与 image layout 转换：解码 `vkCmdPipelineBarrier`（阶段/访问掩码、buffer/image barrier、旧→新 layout + 图像名、前后相邻的 draw/dispatch 事件），并给出能明确判断的问题：`broad_stage_mask`（ALL_COMMANDS/ALL_GRAPHICS，tile GPU 上会串行化 vertex/fragment）、`discards_written_content`（对本帧已写过的图像用 UNDEFINED 旧 layout=丢弃内容，典型的黑/花贴图原因，用 `get_resource_usage` 核对写入和之后的读取）、`mergeable_barriers`（同一 command buffer 内相邻、中间没有 draw/dispatch/copy/clear/pass 边界）、`layout_thrash`（同一图像一帧内换 ≥4 次 layout）、队列所有权转移。**范围**（结果里的 `scope` 也写明）：只解码旧的 `vkCmdPipelineBarrier`，`Barrier2` 和 `SetEvent/WaitEvents` 只计数放进 `not_analysed`；不模拟 render pass 隐含的 layout 变化，所以错误的 oldLayout 查不出来；不判断掩码组合是否合法——那是验证层的事，用 `get_debug_messages`。实测 test-Vulkan_02：15 个 barrier（7 个 image、14 个 buffer，后者来自 7 个 compute pass），`[111,112,113]` 与 `[359..362]` 可合并，真实 UNDEFINED→COLOR（交换链首次使用）没有误报；barrier 数/image/buffer 总数与原始 `get_api_calls` 数据一致，可合并组经原始调用核对中间没有 work |
| `find_pixels` | 按通道条件找像素（网格采样，带包围盒） |
| `get_cached_message` | 超大结果自动缓存后的分页/搜索；列表按条目分页，超大的非列表字段（如 shader 源码）按行分页；打开新抓帧时清空 |
| （实现说明）`analyze_state_changes` / `diagnose_mobile_risks` | GL 下不逐 DC 回放：`services/gl_stream.py` 读调用流，`services/gl_state.py` 跟踪 glEnable/glBlendFunc/glUseProgram 等还原每个 DC 的状态；discard 检测按 program 缓存。839 DC 的帧 118s→0.2s、115s→13s，结果与回放路径一致。非 GL 仍走回放 |
| `analyze_pass_bandwidth` | 按 FBO 切 tile pass，估算附件 Load/Store 带宽并给出 wasted_store / load_without_clear / render_target_reopened / flush_by_readback / msaa_stored 等问题（GLES 按调用流推断，Vulkan 读 render pass 的 loadOp/storeOp）。issues 最多 max(50, top_n) 条，issue_count / issues_by_type / avoidable_mb 覆盖全部 |
| `estimate_texture_bandwidth` | 贴图采样足迹（`mcp_server/texture_analysis.py`，数据来自 `get_draw_coverage(with_textures=True)` + `list_frame_textures`）；上限估算，每像素最多取一个 texel，不含缓存/各向异性 |
| `estimate_geometry_cost` / `analyze_overdraw` | 顶点预算与过度绘制（`mcp_server/draw_analysis.py`，数据来自 `get_draw_coverage(with_state=True)` + malioc 顶点/像素周期）。覆盖像素/三角形数含背面剔除，阈值已补偿 |
| `frame_performance_report` | 基于快照生成记分卡 + 排序后的优化项（`mcp_server/report.py`）。每类带宽问题保留最重的 3 条，其余合并成一条。百分比只在各自领域内可比，阈值是经验值 |
| `snapshot_metrics` / `diff_snapshots` / `compare_captures` | 指标快照与前后对比。Vulkan 抓帧的快照还带 `sync` 段（`vk_sync.brief`：barrier 数、layout 变化数、可合并数、问题类型），对比里有三行 Vulkan barrier 指标（任一边没有该段就跳过，GLES 与 Vulkan 互比不会出错），`frame_performance_report` 多一个 `sync` 板块（记分卡一行 + 每类问题一条行动，没有字节/周期数字，建议用 `get_gpu_counters repeat=3` 前后测）。shader 用源码 hash 匹配，着色成本差拆成 shader_cost_delta 与 coverage_delta；compare 每取完一个快照立即保存 |
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
- 请求里带 `deadline`（客户端不再等待的时刻，epoch 秒）。扩展取走请求时若已过期（超过 2 s 时钟余量）就丢弃且不回应：客户端被杀掉来不及撤回文件时，不会再白白执行（可能是个几分钟的 `get_draw_coverage`）并拖慢仍在等待的客户端。没有 `deadline` 的旧客户端照常处理。已经开始执行的请求无法取消
- 旧协议（`request.json` + `lock` + `response.json`）仍然支持，供未重启的旧客户端使用
- 轮询间隔：100ms（RenderDoc 侧）
- 崩溃时 faulthandler 写到 IPC 目录下的 `fault.log`

## 开发说明

- RenderDoc 内置 Python 没有 socket/QtNetwork 模块，所以用文件 IPC
- 扩展只用 Python 3.6 标准库语法（官方构建是 3.6）
- 访问 ReplayController 通过 `BlockInvoke`。有 PySide2 时 QTimer 在 UI 线程轮询；没有 PySide2 时由工作线程轮询，
  再用 `MiniQtHelper.InvokeOntoUIThread` 交给 UI 线程（传入的回调是创建一次后一直持有的对象：qrenderdoc 的回调胶水
  在不持有 GIL 时修改它的引用计数）
- `BlockInvoke` 会吞掉回调里抛出的异常（调用正常返回，结果留空）。门面的 `_invoke` 经 `utils/invoke.py` 的 `guarded_invoke` 捕获后在调用线程重新抛出，工具出错就是错误而不是 `null`；服务里仍有的 `result["error"]` 写法保持有效
- "event 的 color0/depth 是哪张图"统一走 `services/targets.py` 的 `resolve_target`（D3D/GL 读 `action.outputs`，Vulkan 走 render pass 模型），`pick_pixel` / `save_texture` / `read_texture_stats` / `pixel_history` / `find_pixels` / `render_overlay` 共用
- 枚举名用整数表（`utils/helpers.py` 的 `_ENUM_TABLES`），不依赖各构建 SWIG 枚举的反射方式
- 更新扩展后要重启 RenderDoc（qrenderdoc 会一直持有已加载的模块）
- 测试：`python -m unittest discover -s tests`（离线）；`scripts/e2e_check.py gles|vulkan`（需已打开对应抓帧）

## 参考链接

- [FastMCP](https://github.com/jlowin/fastmcp)
- [RenderDoc Python API](https://renderdoc.org/docs/python_api/index.html)
- [RenderDoc Extension Registration](https://renderdoc.org/docs/how/how_python_extension.html)
