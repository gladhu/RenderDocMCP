# 声明

本仓库基于 https://github.com/halby24/RenderDocMCP 进行功能扩展，扩展了以下功能：

1. 所有图形 API 的 buffer content 获取
2. 远程设备回放（`open_capture(use_remote=True)`，overlay / 直方图 / 读像素走真机 GPU）
3. 移动端着色开销分析（coverage、malioc / aoc、材质 uniform 折叠）
4. 无 PySide2 的自定义 RenderDoc 构建也能启动 MCP Bridge

下文是原仓库 README 的中文版翻译，并已按当前 36 个 MCP 工具和 4 个 Prompt 补全。

# RenderDoc MCP 服务器

作为 RenderDoc UI 扩展运行的 MCP 服务器。使 AI 助手能够访问 RenderDoc 的捕获数据，辅助图形调试和移动端性能分析。

## 架构

```
Claude/AI 客户端 (stdio)
        │
        ▼
MCP 服务器进程 (Python + FastMCP 2.0)
        │ 基于文件的 IPC (%TEMP%/renderdoc_mcp/)
        ▼
RenderDoc 进程 (扩展)
```

由于 RenderDoc 内置的 Python 没有 socket 模块，因此通过基于文件的 IPC 进行通信。

## 安装配置

### 1. 安装 RenderDoc 扩展

```bash
python scripts/install_extension.py
```

扩展将安装到 `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp_bridge`。

### 2. 在 RenderDoc 中启用扩展

1. 启动 RenderDoc
2. 打开 Tools > Manage Extensions
3. 启用 "RenderDoc MCP Bridge"

### 3. 安装 MCP 服务器

```bash
uv tool install
uv tool update-shell  # 添加到 PATH
```

重启终端后即可使用 `renderdoc-mcp` 命令。

> **注意**：添加 `--editable` 参数后，源代码的修改会立即生效（开发时很方便）。
> 如果作为稳定版安装，请使用 `uv tool install .`。

### 4. 配置 MCP 客户端

#### Claude Desktop

在 `claude_desktop_config.json` 中添加：

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

#### Claude Code

在 `.mcp.json` 中添加：

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

## 使用方法

1. 启动 RenderDoc，启用 "RenderDoc MCP Bridge" 扩展
2. 通过 MCP 打开抓帧，或先在 RenderDoc 里打开 `.rdc`
3. 移动端抓帧：在界面左下角 Replay Context 选择手机（`adb://...`），再用 `open_capture(..., use_remote=True)`
4. 通过 MCP 客户端访问数据

> qrenderdoc 在回放线程里访问到 UI 线程的对象时会访问冲突直接退出（故障日志在 `%TEMP%\rdmcp_fault.log`）。开发扩展时踩到过两次：在回放线程里读 `ctx.GetAction(...).events`、把 `pipe.GetVBuffers()` 的返回值存进变量再遍历。需要 action 时请用 `controller.GetRootActions()` 取得的对象，遍历返回列表时直接 `for`。崩溃后需要重启 RenderDoc 并重新打开抓帧。另外，qrenderdoc 会一直持有已加载的旧版扩展模块，更新扩展后需要重启 RenderDoc。
>
> `open_capture` 之后不必固定等待：反复调用只读的 `get_replay_context`，再用 `get_capture_status` 探测回放线程，响应了就可以继续。

## MCP 工具列表

共 44 个工具，另有 4 个分析 Prompt。

### 会话 / 抓帧

| 工具 | 说明 |
|------|------|
| `list_captures` | 列出指定目录内的 `.rdc`（文件名、路径、大小、修改时间） |
| `open_capture` | 打开抓帧（会关闭当前已打开的抓帧）。`use_remote=True` 强制真机回放，`False` 强制本机，省略则自动检测 |
| `get_capture_status` | 检查是否已加载抓帧，以及 API 类型 |
| `get_replay_context` | 当前回放设备：是否已连远程、主机名、是否本机、抓帧是否本地 |

### 帧导航 / 搜索

| 工具 | 说明 |
|------|------|
| `get_draw_calls` | 层级列出绘制调用，可按 marker、event 范围、flags 过滤 |
| `get_api_calls` | 抓帧里的原始 API 调用及参数：按 `event_id`（该 action 之前的状态设置）或 `start_event`+`end_event` 取，`name_filter` 过滤函数名（如 `Uniform`、`Bind`）。能看到 `glUniform*` 的实际数值 |
| `get_cached_message` | 读取因过大而被缓存的工具结果（见下文「超大结果缓存」），可翻页、按集合选择、搜索 |
| `get_frame_summary` | 帧统计：API、DC/Dispatch/Clear/Copy/Present/marker 数量、顶层 marker、资源数 |
| `get_renderpass_list` | 划分 RenderPass（PassBoundary → 直接子 DC 的 marker，子 marker 单独成 Pass → RT 切换） |
| `get_pass_detail` | 指定 Pass 内的 DC 列表和附件 |
| `estimate_texture_bandwidth` | 贴图采样足迹：每张贴图 min(贴图大小, 采样像素数×每 texel 字节) 的上限，找出未压缩的大贴图、利用率很低的大贴图（忽略图集）、被整张读取的 RT。不是实测带宽（不含缓存/过滤） |
| `estimate_geometry_cost` | 顶点/三角形预算：按 marker 完整路径、顶点 shader、DC 汇总，附 VS 编译周期；标出顶点占比过高的 pass 和微小三角形（<4 px/三角形）。需要真机回放 |
| `analyze_overdraw` | 按渲染目标拆分不透明 / late-Z / 透明：不透明被遮挡比例及贡献最大的 DC、late-Z 浪费的片元周期、透明平均层数、不透明 DC 排在透明之后。「混合开启但 ONE/ZERO」按不透明处理 |
| `frame_performance_report` | 一次调用出性能报告：各领域记分卡（带宽/着色/几何/过度绘制/提交/风险；`include_draw_analysis=True` 默认开启，约 8 分钟/帧）+ 按影响排序的优化项（带 event/marker 证据）；传 `snapshot=` 可基于已存快照秒出 |
| `snapshot_metrics` | 把当前抓帧浓缩成指标快照（DC/状态切换/贴图内存/带宽/shader 着色成本/风险）并存成 JSON |
| `diff_snapshots` | 对比两份快照：指标表(better/worse/same)、带宽问题 解决/新增/持续、按 shader 拆分「shader 本身变化」和「镜头覆盖变化」 |
| `compare_captures` | 依次打开两个 rdc，各取快照并对比（优化前后验证一步到位，约 10 分钟/抓帧） |
| `analyze_pass_bandwidth` | 每个 tile pass 的附件 Load/Store 带宽（clear/invalidate 是否跳过、Store 后是否还有人读、RT 被打断重开、blit/readback 强制 flush），输出严重度排序的问题列表。OpenGL ES 与 Vulkan（Vulkan 直接读 loadOp/storeOp） |
| `get_draw_call_details` | 单个 DC 的顶点数、输出等详情 |
| `find_draws_by_shader` | 按 shader 名（部分匹配）反查 DC，可限定 stage |
| `find_draws_by_texture` | 按贴图名（部分匹配）反查 DC，搜索 SRV / UAV / RT |
| `find_draws_by_resource` | 按 ResourceId（完全匹配）反查 DC |
| `get_resource_usage` | 资源在整帧的读写/Clear 使用历史 |
| `list_shaders` | 帧内唯一 shader 清单（可按 stage），带 DC 次数 |

### 管线 / 资源

| 工具 | 说明 |
|------|------|
| `get_pipeline_state` | 指定事件的完整管线：各 stage shader、SRV/UAV、sampler、常量缓冲、RT、viewport、IA |
| `get_shader_info` | 指定 stage 的反汇编、常量缓冲取值、资源绑定 |
| `read_constant_buffer` | 定点读取某个 stage 的一个常量缓冲（变量名 + 数值，`filter` 按变量名子串过滤）。`slot` 是 shader 常量块的**索引**或 cbuffer 名字（精确 / 子串 / `cb1` 写法），不是 API 绑定点；`slot` 留空则列出该 stage 全部块的 索引-名字-大小 |
| `get_buffer_contents` | 读取 buffer（可指定 offset/length，Base64） |
| `list_buffers` | 整帧 buffer 清单（id / 字节长度 / 名字）。`group_by_size` / `group_by_name` 出直方图，`unused_only` 列出从未被绑定、拷贝或清除的 buffer，`with_usage` 追加使用次数与首末 event |
| `get_buffer_bindings_at_event` | 单个 DC 绑定的 buffer：已绑定的顶点缓冲、索引缓冲，以及各 stage 常量缓冲（按反射索引，含绑定的资源与字节范围） |
| `get_texture_info` | 贴图元数据（尺寸、格式、mip 等） |
| `get_texture_data` | 贴图像素数据（mip / slice / 3D slice，Base64） |
| `list_frame_textures` | 帧内贴图清单：尺寸、格式、显存估算、读写/Clear 次数、是否 RT |
| `get_texture_detail` | 单张贴图元数据 + 完整 usage 事件列表 |

### GPU 计时

| 工具 | 说明 |
|------|------|
| `get_action_timings` | 逐 action 的 GPU 时间。硬件/驱动可能不可用；Mali/Adreno/PowerVR 上逐 DC 计时会因 tile flush 偏大，移动端请用 `estimate_shading_cost` |

### 性能分析

以下工具都通过 replay controller 执行。用 `open_capture(use_remote=True)` 在真机上回放时，overlay、直方图和像素读取都由手机 GPU 完成，可以避开 PC 回放移动端 GLES 抓帧时的画面错误。

| 工具 | 说明 |
|------|------|
| `get_draw_coverage` | 逐 DC 统计通过深度测试的像素数和被遮挡的像素数（深度测试 overlay + GPU 直方图，不回传图像） |
| `get_shader_usage` | 按像素着色器给 DC 分组：DC 数、图元数、常用贴图（用来识别是哪个材质） |
| `get_shader_source` | 导出 GLSL 源码，可以把 UnityPerMaterial 的实际取值折叠成 `#define`，还原该材质实际执行的分支路径 |
| `analyze_shader_cost` | 对选中的 DC 跑离线编译器（默认按材质参数折叠）。`compiler="mali"`（默认）用 malioc：各流水线单元的周期数、寄存器、线程占用率、fp16 比例、late ZS / discard；`compiler="adreno"` 用 aoc：各类指令数、寄存器、fiber 占用率、scratch 溢出 |
| `estimate_shading_cost` | 用「像素数 × 每像素开销」估算各 Shader 的着色开销占比，用于排热点、对比优化前后。Mali 用 malioc 周期，Adreno 用 aoc 指令数 |
| `save_texture` | 保存贴图或某个事件的 color / depth RT 到本地图片，远程回放时同样可用 |
| `pick_pixel` | 读取某个事件之后某个像素的颜色或深度 |
| `find_pixels` | 按通道条件（`min_/max_` r/g/b/a、`has_nan/inf/negative`）在渲染目标里找像素，返回命中数、包围盒和命中点。区域不超过 `max_probes` 时穷举，否则等间距采样（`exhaustive` 标明） |
| `diagnose_mobile_risks` | 扫描精度 / 性能 / 兼容性 / GPU 特性风险（R11G11B10、D16、超大贴图、过多 Clear、discard） |
| `pixel_history` | 某个像素被哪些 DC 修改，以及 depth/stencil/discard 失败原因（默认最多 200 条） |
| `read_texture_stats` | GPU min/max，深度目标按 Depth 解释；标记 NaN / Inf / 负值 / 全 0 |
| `sample_pixel_region` | 网格采样一块区域，报告异常热点 |
| `analyze_state_changes` | 统计连续 DC 的 shader/blend/depth/cull/RT 切换，找出可合批区间 |
| `compare_pipeline_states` | 对比两个 event 的管线状态，只返回不同的字段（另附 `batching`：把差异归为 shader / textures / 常量缓冲资源 / 常量缓冲偏移 / mesh / 渲染状态 / 目标，并说明对合批意味着什么）：shader、纹理（按 unit）、常量缓冲（资源+偏移/大小）、顶点/索引缓冲、RT、拓扑、blend/depth/stencil/rasterizer。回答「这两个 DC 看起来一样，为什么没合批」 |
| `stat_instancing_efficiency` | 整帧实例化统计：实例化 DC 数与占比、实例数分布、「浪费的实例化」（实例数 ≤ 2），按前两层 marker（即各 pass）分桶；只读 action 树，很快 |
| `get_post_vs_data` | 按索引顺序返回变换后顶点；只解码 float/half |
| `export_mesh` | 按索引缓冲把 post-VS 网格导出为 OBJ（float/half） |

> malioc 需要安装 Arm Performance Studio（`malioc` 在 PATH 里），或者设置环境变量 `MALIOC_PATH`。`compiler="mali"` 时 `gpu` 填真机的 GPU 型号，例如 `Mali-G610`。
>
> Adreno 分析需要 Qualcomm Adreno SDK 里的 `aoc`（在 PATH 里，或设置 `AOC_PATH`）。`compiler="adreno"` 时 `gpu` 填架构，例如 `a650`（默认）、`a730`、`a740`。aoc 没有周期模型，开销用总指令数表示，只能在 Adreno shader 之间比较，不能和 Mali 的周期数互比。
>
> `get_draw_coverage` 的 overlay 忽略 shader discard，alpha test 的 DC 可能略偏大。
>
> `read_constant_buffer` 里整型 / bool 变量的 `value` 目前为空（浮点正常）：现有序列化器引用了本版 RenderDoc 没有的 `rd.VarType.Int`，`get_pipeline_state` 里的常量缓冲同样受影响。
>
> `list_buffers(unused_only=True)` 只依据 `GetUsage`，GLES 回放下可能漏记 SSBO / transform feedback 之类的用法，删除 buffer 前请再用 `find_draws_by_resource` 复核。
>
> `compare_pipeline_states` 在 GLES 回放下 blend/depth 等固定管线状态走 `GetGLPipelineState()`：blend 枚举是数字，8 个 RT 槽会各报一遍（`diff_count` 偏大，以 `differences` 内容为准）；stencil / rasterizer 读不到时会静默省略。常量缓冲的偏移不同（同一 buffer 的不同偏移）是 SRP Batcher 的正常形态，不代表没合批。
>
> `analyze_shader_cost` 默认 `fold_uniforms=True`，对 uniform 分支准确，但对无分支 shader 会把 `lerp(x, y, 0)` 这类运算也常量折叠掉，可能低估真实开销；无分支材质请设 `fold_uniforms=False`。

### 参数容错

LLM 常把整型参数传成空串 `""` 或数字字符串 `"5"`，stage 写成缩写，原本会在进入工具之前就被 schema 拒绝并陷入重试死循环。现在在校验阶段清洗，工具的 JSON schema 不变（仍是 integer / 枚举）：

- 可选或带默认值的整型：`""` / `null` 回落到默认值；必填整型（`event_id`、`x`、`y` 等）传空值会得到带示例的明确错误
- `"5"`、`" 5 "`、`5.0` 都按 5 处理；`event_ids` 接受 `[1,2]`、`"1,2,3"`、`"[1,2]"` 或单个数字
- `stage` 大小写不敏感，接受 `vs` / `ps` / `cs` / `gs` / `hs` / `ds` / `frag` 等缩写，统一为 `vertex` / `pixel` / `compute` 等全名

### 超大结果缓存

工具结果超过 20000 字符（环境变量 `RENDERDOC_MCP_MAX_RESPONSE_CHARS`）时不直接返回，而是缓存在 MCP 服务器进程里（最近 64 条，`RENDERDOC_MCP_CACHE_MAX_ENTRIES`），返回 `{"status":"cached","message_id":...}`，附各列表的大小和前几项预览。再用 `get_cached_message(message_id, page, page_size, collection, search)` 翻页或搜索。例如整帧 `get_draw_calls` 在样本抓帧上是 118256 字符，会变成不到 1000 字符的信封。`get_texture_data` / `get_buffer_contents` 的 Base64 结果不缓存。进程重启或被淘汰后 id 失效，需要重跑原工具。

### 分析提示词（MCP Prompt）

给 AI 的固定工作流，客户端可直接选用：

| Prompt | 说明 |
|------|------|
| `full_frame_analysis` | 完整帧分析：回放设备 → Pass / 贴图 / shader → 着色开销 → 移动端风险 |
| `debug_draw_call` | 单个 DC：管线、绑定、源码、malioc、输出 |
| `analyze_performance` | 性能：Pass 热点、coverage / shading cost、状态切换、合批 |
| `find_rendering_issue` | 按问题描述排查：Pass → 像素统计 / history → 精度诊断 |

### 推荐的移动端分析流程

```
# 1. 在 RenderDoc 界面左下角 Replay Context 选择手机（adb://...）
get_replay_context()
open_capture(capture_path="D:\\farm\\a.rdc", use_remote=True)
save_texture(output_path="D:\\tmp\\frame.png", event_id=<最后一个事件>)   # 先和真机截图对照
estimate_shading_cost(marker_filter="DrawOpaqueObjects", gpu="Mali-G610")
analyze_shader_cost(event_ids=[5503], gpu="Mali-G610")                  # 单个材质的详细开销
get_shader_source(event_id=5503, fold_uniforms=True, output_path="D:\\tmp\\plant.frag")
```

## 使用示例

### 打开抓帧

```
list_captures(directory="D:\\captures")
get_replay_context()
open_capture(capture_path="D:\\captures\\game.rdc")
open_capture(capture_path="D:\\farm\\a.rdc", use_remote=True)
get_capture_status()
```

### 获取绘制调用列表

```
get_draw_calls(include_children=true)

get_draw_calls(
    include_children=true,
    marker_filter="Camera.Render",
    exclude_markers=["GUI.Repaint", "UIR.DrawChain"],
    event_id_min=7372,
    event_id_max=7600,
    only_actions=true,
    flags_filter=["Drawcall", "Dispatch"]
)

get_frame_summary()
get_renderpass_list(sort_by="draw_count")
get_pass_detail(pass_index=0)
get_draw_call_details(event_id=123)
```

### 按资源反查绘制调用

```
find_draws_by_shader(shader_name="Toon", stage="pixel")
find_draws_by_texture(texture_name="CharacterSkin")
find_draws_by_resource(resource_id="ResourceId::12345")
get_resource_usage(resource_id="ResourceId::12345")
list_shaders(stage="pixel", top_n=20)
```

### 获取着色器 / 管线信息

```
get_shader_info(event_id=123, stage="pixel")
get_pipeline_state(event_id=123)
```

### 获取纹理数据

```
# 获取 2D 纹理的 mip 0
get_texture_data(resource_id="ResourceId::123")

# 获取特定的 mip 级别
get_texture_data(resource_id="ResourceId::123", mip=2)

# 获取立方体贴图的特定面 (0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-)
get_texture_data(resource_id="ResourceId::456", slice=3)

# 获取 3D 纹理的特定深度切片
get_texture_data(resource_id="ResourceId::789", depth_slice=5)

get_texture_info(resource_id="ResourceId::123")
list_frame_textures(sort_by="size", top_n=20)
list_frame_textures(rt_only=true, sort_by="usage")
get_texture_detail(resource_id="ResourceId::123")
```

### 部分获取缓冲区数据

```
# 获取整个缓冲区
get_buffer_contents(resource_id="ResourceId::123")

# 从偏移量 256 开始获取 512 字节
get_buffer_contents(resource_id="ResourceId::123", offset=256, length=512)
```

### 常量缓冲 / buffer / 合批分析

```
# 先列出 pixel stage 的常量块（索引-名字-大小），再按索引或名字读取
read_constant_buffer(event_id=5503, stage="ps")
read_constant_buffer(event_id=5503, stage="ps", slot="UnityPerMaterial")
read_constant_buffer(event_id=5503, stage="vs", slot=1)

# 整帧 buffer 全貌：按大小聚类、列出从未被使用的 buffer
list_buffers(group_by_size=true)
list_buffers(unused_only=true, limit=20)
list_buffers(name_filter="SM_", size_min=1024, with_usage=true)
get_buffer_bindings_at_event(event_id=5503)

# 为什么两个 DC 没合批？只看不同的字段
compare_pipeline_states(event_id_a=5503, event_id_b=5511)
stat_instancing_efficiency()
stat_instancing_efficiency(marker_filter="DrawOpaqueObjects", top_n=5)
```

### GPU 计时

```
get_action_timings()
get_action_timings(event_ids=[100, 200, 300])
get_action_timings(marker_filter="Camera.Render", exclude_markers=["GUI.Repaint"])
```

### 性能分析

```
get_draw_coverage(marker_filter="DrawOpaqueObjects")
get_draw_coverage(event_ids=[5503, 5510], summary_only=true)
get_shader_usage(marker_filter="DrawOpaqueObjects")

get_shader_source(event_id=5503, fold_uniforms=true, output_path="D:\\tmp\\plant.frag")
analyze_shader_cost(event_ids=[5503], gpu="Mali-G610")
estimate_shading_cost(marker_filter="DrawOpaqueObjects", gpu="Mali-G610")

save_texture(output_path="D:\\tmp\\frame.png", event_id=9000)
save_texture(output_path="D:\\tmp\\depth.png", event_id=9000, target="depth")
pick_pixel(x=640, y=360, event_id=9000, target="color0")
diagnose_mobile_risks()
diagnose_mobile_risks(check_categories=["precision"], severity_filter="high")
pixel_history(x=640, y=360, event_id=9000, target="color0")
read_texture_stats(event_id=9000, target="color0")
sample_pixel_region(event_id=9000, sample_count=256)
analyze_state_changes(marker_filter="DrawOpaqueObjects")
get_post_vs_data(event_id=5503, stage="vsout", max_vertices=32)
export_mesh(event_id=5503, output_path="D:\\tmp\\mesh.obj")
```

## 系统要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- RenderDoc 1.20+
- 移动端着色分析：Arm Performance Studio（`malioc`，或设置 `MALIOC_PATH`）；Adreno 需 Qualcomm Adreno SDK（`aoc`，或设置 `AOC_PATH`）
- 远程回放：RenderDoc 已通过 ADB 等连上目标设备，并在 Replay Context 中选中该设备

> 已在 Windows + DirectX 11，以及 Windows + 移动端 GLES 远程回放场景下使用。Linux/macOS + Vulkan/OpenGL 可能也能运行，但尚未系统验证。

## 许可证

MIT
