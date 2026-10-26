# 声明

本仓库基于 https://github.com/halby24/RenderDocMCP 进行功能扩展，扩展了以下功能：

1. 现在支持所有图形API的buffer content获取
2. remote-device-replay

下文是原仓库README的中文版翻译：

# RenderDoc MCP 服务器

作为 RenderDoc UI 扩展运行的 MCP 服务器。使 AI 助手能够访问 RenderDoc 的捕获数据，辅助图形调试。

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

1. 启动 RenderDoc，打开捕获文件 (.rdc)
2. 通过 MCP 客户端（如 Claude）访问 RenderDoc 的数据

## MCP 工具列表

| 工具 | 说明 |
|------|------|
| `get_capture_status` | 检查捕获的加载状态 |
| `get_draw_calls` | 以层级结构获取绘制调用列表 |
| `get_draw_call_details` | 获取特定绘制调用的详细信息 |
| `get_shader_info` | 获取着色器源代码及常量缓冲区的值 |
| `get_buffer_contents` | 获取缓冲区内容 (Base64) |
| `get_texture_info` | 获取纹理的元数据 |
| `get_texture_data` | 获取纹理的像素数据 (Base64) |
| `get_pipeline_state` | 获取管线状态 |

### 性能分析工具（本地扩展）

以下工具都通过 replay controller 执行。用 `open_capture(use_remote=True)` 在真机上回放时，overlay、直方图和像素读取都由手机 GPU 完成，可以避开 PC 回放移动端 GLES 抓帧时的画面错误。

| 工具 | 说明 |
|------|------|
| `get_draw_coverage` | 逐 DC 统计通过深度测试的像素数和被遮挡的像素数（深度测试 overlay + GPU 直方图，不回传图像） |
| `get_shader_usage` | 按像素着色器给 DC 分组：DC 数、图元数、常用贴图（用来识别是哪个材质） |
| `get_shader_source` | 导出 GLSL 源码，可以把 UnityPerMaterial 的实际取值折叠成 `#define`，还原该材质实际执行的分支路径 |
| `analyze_shader_cost` | 对选中的 DC 跑 malioc（默认按材质参数折叠）：各流水线单元的周期数、寄存器、线程占用率、fp16 比例、late ZS / discard |
| `estimate_shading_cost` | 用"像素数 × malioc 每像素周期"估算各 Shader 的着色开销占比，用于排热点、对比优化前后 |
| `save_texture` | 保存贴图或某个事件的 color / depth RT 到本地图片，远程回放时同样可用 |
| `pick_pixel` | 读取某个事件之后某个像素的颜色或深度 |

> malioc 需要安装 Arm Performance Studio（malioc 在 PATH 里），或者设置环境变量 `MALIOC_PATH`。`gpu` 参数要填真机的 GPU 型号，例如 `Mali-G610`。
>
> 在分块渲染的 GPU（Mali / Adreno）上，`get_action_timings` 的逐 DC 计时会把整块 tile 的读写都算进去，数值偏大且加起来不等于帧时间。这类抓帧请用 `estimate_shading_cost`。

### 推荐的移动端分析流程

```
# 1. 在 RenderDoc 界面左下角 Replay Context 选择手机（adb://...）
open_capture(capture_path="D:\\farm\\a.rdc", use_remote=True)
save_texture(output_path="D:\\tmp\\frame.png", event_id=<最后一个事件>)   # 先和真机截图对照
estimate_shading_cost(marker_filter="DrawOpaqueObjects", gpu="Mali-G610")
analyze_shader_cost(event_ids=[5503], gpu="Mali-G610")                  # 单个材质的详细开销
get_shader_source(event_id=5503, fold_uniforms=True, output_path="D:\\tmp\\plant.frag")
```

## 使用示例

### 获取绘制调用列表

```
get_draw_calls(include_children=true)
```

### 获取着色器信息

```
get_shader_info(event_id=123, stage="pixel")
```

### 获取管线状态

```
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
```

### 部分获取缓冲区数据

```
# 获取整个缓冲区
get_buffer_contents(resource_id="ResourceId::123")

# 从偏移量 256 开始获取 512 字节
get_buffer_contents(resource_id="ResourceId::123", offset=256, length=512)
```

## 系统要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- RenderDoc 1.20+

> **注意**：仅在 Windows + DirectX 11 环境下进行过测试验证。
> 在 Linux/macOS + Vulkan/OpenGL 环境下可能也能运行，但尚未验证。

## 许可证

MIT
