# 声明

本仓库基于 https://github.com/halby24/RenderDocMCP 进行功能扩展，扩展了以下功能：

1. 所有图形 API 的 buffer content 获取
2. 远程设备回放（`open_capture(use_remote=True)`，overlay / 直方图 / 读像素走真机 GPU）
3. 移动端着色开销分析（coverage、malioc、材质 uniform 折叠）
4. 无 PySide2 的自定义 RenderDoc 构建也能启动 MCP Bridge

下文是原仓库 README 的中文版翻译，并已按当前 23 个 MCP 工具补全。

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

## MCP 工具列表

共 23 个工具。

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
| `get_frame_summary` | 帧统计：API、DC/Dispatch/Clear/Copy/Present/marker 数量、顶层 marker、资源数 |
| `get_draw_call_details` | 单个 DC 的顶点数、输出等详情 |
| `find_draws_by_shader` | 按 shader 名（部分匹配）反查 DC，可限定 stage |
| `find_draws_by_texture` | 按贴图名（部分匹配）反查 DC，搜索 SRV / UAV / RT |
| `find_draws_by_resource` | 按 ResourceId（完全匹配）反查 DC |

### 管线 / 资源

| 工具 | 说明 |
|------|------|
| `get_pipeline_state` | 指定事件的完整管线：各 stage shader、SRV/UAV、sampler、常量缓冲、RT、viewport、IA |
| `get_shader_info` | 指定 stage 的反汇编、常量缓冲取值、资源绑定 |
| `get_buffer_contents` | 读取 buffer（可指定 offset/length，Base64） |
| `get_texture_info` | 贴图元数据（尺寸、格式、mip 等） |
| `get_texture_data` | 贴图像素数据（mip / slice / 3D slice，Base64） |

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
| `analyze_shader_cost` | 对选中的 DC 跑 malioc（默认按材质参数折叠）：各流水线单元的周期数、寄存器、线程占用率、fp16 比例、late ZS / discard |
| `estimate_shading_cost` | 用「像素数 × malioc 每像素周期」估算各 Shader 的着色开销占比，用于排热点、对比优化前后 |
| `save_texture` | 保存贴图或某个事件的 color / depth RT 到本地图片，远程回放时同样可用 |
| `pick_pixel` | 读取某个事件之后某个像素的颜色或深度 |
| `diagnose_mobile_risks` | 扫描精度 / 性能 / 兼容性 / GPU 特性风险（R11G11B10、D16、超大贴图、过多 Clear、discard） |

> malioc 需要安装 Arm Performance Studio（`malioc` 在 PATH 里），或者设置环境变量 `MALIOC_PATH`。`gpu` 参数要填真机的 GPU 型号，例如 `Mali-G610`。
>
> `get_draw_coverage` 的 overlay 忽略 shader discard，alpha test 的 DC 可能略偏大。
>
> `analyze_shader_cost` 默认 `fold_uniforms=True`，对 uniform 分支准确，但对无分支 shader 会把 `lerp(x, y, 0)` 这类运算也常量折叠掉，可能低估真实开销；无分支材质请设 `fold_uniforms=False`。

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
get_draw_call_details(event_id=123)
```

### 按资源反查绘制调用

```
find_draws_by_shader(shader_name="Toon", stage="pixel")
find_draws_by_texture(texture_name="CharacterSkin")
find_draws_by_resource(resource_id="ResourceId::12345")
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
```

### 部分获取缓冲区数据

```
# 获取整个缓冲区
get_buffer_contents(resource_id="ResourceId::123")

# 从偏移量 256 开始获取 512 字节
get_buffer_contents(resource_id="ResourceId::123", offset=256, length=512)
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
```

## 系统要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- RenderDoc 1.20+
- 移动端着色分析：Arm Performance Studio（`malioc`），或设置 `MALIOC_PATH`
- 远程回放：RenderDoc 已通过 ADB 等连上目标设备，并在 Replay Context 中选中该设备

> 已在 Windows + DirectX 11，以及 Windows + 移动端 GLES 远程回放场景下使用。Linux/macOS + Vulkan/OpenGL 可能也能运行，但尚未系统验证。

## 许可证

MIT
