# RenderDoc MCP Server

RenderDoc UI拡張機能として動作するMCPサーバー。AIアシスタントがRenderDocのキャプチャデータにアクセスし、DirectX 11/12のグラフィックスデバッグを支援する。

## アーキテクチャ

**ハイブリッドプロセス分離方式**:

```
Claude/AI Client (stdio)
        │
        ▼
MCP Server Process (標準Python + FastMCP 2.0)
        │ File-based IPC (%TEMP%/renderdoc_mcp/)
        ▼
RenderDoc Process (Extension + File Polling)
```

## プロジェクト構成

```
RenderDocMCP/
├── mcp_server/                        # MCPサーバー
│   ├── server.py                      # FastMCPエントリーポイント
│   ├── config.py                      # 設定
│   └── bridge/
│       └── client.py                  # ファイルベースIPCクライアント
│
├── renderdoc_extension/               # RenderDoc拡張機能
│   ├── __init__.py                    # register()/unregister()
│   ├── extension.json                 # マニフェスト
│   ├── socket_server.py               # ファイルベースIPCサーバー
│   ├── request_handler.py             # リクエスト処理
│   └── renderdoc_facade.py            # RenderDoc APIラッパー
│
└── scripts/
    └── install_extension.py           # 拡張機能インストール
```

## MCPツール

| ツール名 | 説明 |
|---------|------|
| `list_captures` | 指定ディレクトリ内の.rdcファイル一覧を取得 |
| `open_capture` | キャプチャファイルを開く（既存キャプチャは自動で閉じる） |
| `get_capture_status` | キャプチャ読込状態確認 |
| `get_draw_calls` | ドローコール一覧（階層構造、フィルタリング対応） |
| `get_frame_summary` | フレーム全体の統計情報（ドローコール数、マーカー一覧等） |
| `find_draws_by_shader` | シェーダー名でドローコールを逆引き検索 |
| `find_draws_by_texture` | テクスチャ名でドローコールを逆引き検索 |
| `find_draws_by_resource` | リソースIDでドローコールを逆引き検索 |
| `get_draw_call_details` | 特定ドローコールの詳細 |
| `get_action_timings` | アクションのGPU実行時間を取得 |
| `get_shader_info` | シェーダーソース/定数バッファ |
| `get_buffer_contents` | バッファデータ取得（オフセット/長さ指定可） |
| `get_texture_info` | テクスチャメタデータ |
| `get_texture_data` | テクスチャピクセルデータ取得（mip/slice/3Dスライス対応） |
| `get_pipeline_state` | パイプライン状態全体 |
| `read_constant_buffer` | 定数バッファを1つだけ読む。slot は reflection 上のブロック index か cbuffer 名（API のバインド点ではない）。slot 省略で index/名前/サイズの一覧 |
| `list_buffers` | バッファ一覧（サイズ/名前フィルタ、group_by_size/name、unused_only、with_usage） |
| `get_buffer_bindings_at_event` | 1 DC にバインドされた VB/IB/定数バッファ |
| `compare_pipeline_states` | 2 つのイベントのパイプライン状態を比較し、異なるフィールドだけ返す（バッチされなかった理由の調査用） |
| `get_api_calls` | 生の API 呼び出しとパラメータ（event_id か start/end_event、name_filter） |
| `find_pixels` | チャンネル条件に合うピクセルを探す（グリッドサンプリング、バウンディングボックス付き） |
| `get_cached_message` | 大きすぎて自動キャッシュされた結果のページング/検索 |
| （实现说明）`analyze_state_changes` / `diagnose_mobile_risks` | GL 下不再逐 DC 回放：`services/gl_stream.py` 读调用流，`services/gl_state.py` 跟踪 glEnable/glBlendFunc/glUseProgram 等还原每个 DC 的状态；discard 检测按 program 缓存。839 DC 的帧 118s→0.2s、115s→13s，结果与回放路径一致。非 GL 仍走回放 |
| `analyze_pass_bandwidth` | 按 FBO 切 tile pass，估算附件 Load/Store 带宽并给出 wasted_store / load_without_clear / render_target_reopened / flush_by_readback / msaa_stored 等问题（OpenGL ES 按调用流推断，Vulkan 读 render pass 的 loadOp/storeOp） |
| `estimate_texture_bandwidth` | 贴图采样足迹（`mcp_server/texture_analysis.py`，数据来自 `get_draw_coverage(with_textures=True)` + `list_frame_textures`）；上限估算，每像素最多取一个 texel，不含缓存/各向异性 |
| `estimate_geometry_cost` / `analyze_overdraw` | 顶点预算与过度绘制分析（`mcp_server/draw_analysis.py`，数据来自 `get_draw_coverage(with_state=True)` + malioc 顶点/像素周期）。覆盖像素/三角形数含背面剔除，阈值已补偿 |
| `frame_performance_report` | 基于快照生成记分卡 + 排序后的优化项（`mcp_server/report.py`）。百分比只在各自领域内可比（带宽占比 / 着色周期占比 / DC 占比），阈值是经验值，不是实测 |
| `snapshot_metrics` / `diff_snapshots` / `compare_captures` | 指标快照与前后对比。shader 用源码 hash 匹配，着色成本差拆成 shader_cost_delta 与 coverage_delta；compare 每次加载后等 60s（`RENDERDOC_MCP_COMPARE_SETTLE`）再取快照 |
| `stat_instancing_efficiency` | フレーム全体のインスタンシング統計（pass 別、無駄なインスタンス DC 数） |

### get_draw_calls フィルタリングオプション

```python
get_draw_calls(
    include_children=True,      # 子アクションを含める
    marker_filter="Camera.Render",  # このマーカー配下のみ取得
    exclude_markers=["GUI.Repaint", "UIR.DrawChain"],  # 除外するマーカー
    event_id_min=7372,          # event_id範囲の開始
    event_id_max=7600,          # event_id範囲の終了
    only_actions=True,          # マーカーを除外（ドローコールのみ）
    flags_filter=["Drawcall", "Dispatch"],  # 特定フラグのみ
)
```

### キャプチャ管理ツール

```python
# ディレクトリ内のキャプチャファイルを列挙
list_captures(directory="D:\\captures")
# → {"count": 3, "captures": [{"filename": "game.rdc", "path": "...", "size_bytes": 12345, "modified_time": "..."}, ...]}

# キャプチャファイルを開く（既存キャプチャは自動で閉じられる）
open_capture(capture_path="D:\\captures\\game.rdc")
# → {"success": true, "filename": "game.rdc", "api": "D3D11"}
```

### 逆引き検索ツール

```python
# シェーダー名で検索（部分一致）
find_draws_by_shader(shader_name="Toon", stage="pixel")

# テクスチャ名で検索（部分一致）
find_draws_by_texture(texture_name="CharacterSkin")

# リソースIDで検索（完全一致）
find_draws_by_resource(resource_id="ResourceId::12345")
```

### GPU タイミング取得

```python
# 全アクションのタイミングを取得
get_action_timings()
# → {"available": true, "unit": "CounterUnit.Seconds", "timings": [...], "total_duration_ms": 12.5, "count": 150}

# 特定のイベントIDのみ取得
get_action_timings(event_ids=[100, 200, 300])

# マーカーでフィルタリング
get_action_timings(marker_filter="Camera.Render", exclude_markers=["GUI.Repaint"])
```

**注意**: GPUタイミングカウンターはハードウェア/ドライバーによっては利用できない場合があります。
`available: false` が返された場合、そのキャプチャではタイミング情報を取得できません。

## Vulkan 注意事項

- Vulkan 的绘制动作 `outputs/depthOut` 为空，目标属于 render pass：用 `services/draw_targets.py` / `vk_passes.py`
  由调用流得到事件到目标的映射，不要依赖 `action.outputs`。
- 不要读 `tex.creationFlags`：组合标志值（交换链图像）会让 qrenderdoc 在 `enum.py` 里崩溃，交换链按资源名识别。
- Shader 是 SPIR-V（`refl.rawBytes`），没有 GLSL；discard 用 SPIR-V 指令扫描，成本交给 malioc 的 `.spv` 输入。
- 原型开发可以不经 GUI：adb forward 手机远程服务 `localabstract:renderdoc_39920`，用 `renderdoc` 的
  `CreateRemoteServerConnection` + `OpenCapture` 在独立 Python 里回放，更稳定也更快。
- 抓帧用 `scripts/capture_android.py`（debuggable 应用，`run-as` 放入层库，不必降 targetSdk）。
- 多个 qrenderdoc 实例同时存在时用 `RENDERDOC_MCP_IPC_DIR` 给各自独立的 IPC 目录。

## 通信プロトコル

ファイルベースIPC:
- IPCディレクトリ: `%TEMP%/renderdoc_mcp/`
- `request.json`: リクエスト（MCPサーバー → RenderDoc）
- `response.json`: レスポンス（RenderDoc → MCPサーバー）
- `lock`: 書き込み中ロックファイル
- ポーリング間隔: 100ms（RenderDoc側）

## 開発ノート

- RenderDoc内蔵Pythonにはsocket/QtNetworkモジュールがないため、ファイルベースIPCを採用
- RenderDoc拡張機能はPython 3.6標準ライブラリのみ使用
- ReplayControllerへのアクセスは`BlockInvoke`経由で行う

## 参考リンク

- [FastMCP](https://github.com/jlowin/fastmcp)
- [RenderDoc Python API](https://renderdoc.org/docs/python_api/index.html)
- [RenderDoc Extension Registration](https://renderdoc.org/docs/how/how_python_extension.html)
