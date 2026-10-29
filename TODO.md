# TODO

Known problems and limits, found while building and testing the performance-analysis tools
(GLES: Unity / Mali-G610, `8.057.001.108` capture; Vulkan: `test-Vulkan_02` capture, 59 draws, 2 render passes).
Each item says what is known, what is not, and how to tell it is done.

## P0 – stability

- [ ] **qrenderdoc access-violates at random on the replay thread.**
  Seen at: `controller.GetAPIProperties()`, `ctx.APIProps()`, the recursive action-tree walks
  (`stat_instancing_efficiency`, `_select_draws`, `get_draw_calls`'s serializer), `gl_stream.take()` reading
  `action.flags`, and `int(tex.creationFlags)`. Crash trace goes to `%TEMP%/rdmcp_fault.log`.
  - Done so far (workarounds, not a root cause): API properties cached at request entry on the UI thread
    (`Helpers.refresh_api_props`); action attributes read before recursing; swapchain detected by name instead of
    `creationFlags`; GL state rebuilt from the call stream instead of per-draw replay.
  - Not explained: the `gl_stream.take()` crash happens on a read that is already before the recursion, so the
    "read before descending" theory does not cover it.
  - Idea: do all action-tree reads once per request into plain Python data (one walk, no SWIG objects kept),
    then run every analysis on that copy. Also try the standalone replay (`CreateRemoteServerConnection`), which has
    been stable in every run so far, as the analysis backend instead of the GUI extension.
  - Done when: 20 consecutive `open_capture` + full `e2e` runs (GLES and Vulkan) with 0 crashes.
  - Not covered: the earlier "first call right after `open_capture` crashes" cases may have the same cause.

- [ ] **`stat_instancing_efficiency` / `get_draw_calls` still use the walk pattern that crashed.**
  Revisit after the item above; the snapshot calls `stat_instancing_efficiency` again, so a crash there loses a
  whole snapshot (minutes of replay).

## P1 – correctness and coverage

- [ ] **Vulkan: only 2 render passes, 59 draws were ever tested.** Untested on a real capture:
  `vkCmdBeginRendering` (dynamic rendering, synthetic tests only), multi-subpass render passes (attachment role is
  taken from the first reference), MSAA with resolve, a LOAD / reopened render target, secondary command buffers
  across passes.
  - Needs: a heavier Unity Vulkan capture (URP with post-processing / MSAA / shadow cascades). Capture with
    `scripts/capture_android.py`.
- [ ] **Adreno is unverified end to end.** `aoc` only takes GLSL, so Vulkan shaders on Adreno return an explicit error;
  the GLES Adreno path was never run on a Qualcomm capture.
- [ ] **Vulkan shading cost has no uniform folding** (SPIR-V cannot take `#define`s), so branches a material never
  takes still count. The numbers are higher than the GLES numbers for the same material and must not be compared
  across APIs. Idea: specialization constants, or fold in the SPIR-V by rewriting `OpConstant` of the material block.
- [ ] **Vulkan `analyze_state_changes` still replays every draw** (GLES uses the call stream: 118 s -> 0.2 s).
  Port `gl_state` to `vkCmdBindPipeline` / dynamic state, or read it from the pipeline create infos.
- [ ] **Event -> render target mapping ignores subpasses** (`draw_targets.event_targets` takes all colour attachments of
  the render pass).
- [ ] **GL state tracking starts from GL defaults.** State set before the frame and used by the first draws gives a few
  spurious changes at the start. Only the first render target's blend is followed (`glEnablei` index 0).
- [ ] **Micro-triangle share is an upper bound**: no frustum culling and back-facing triangles are in the divisor
  (threshold compensated to 4 px/triangle, severity capped at medium). Needs post-VS data (`get_post_vs_data`) to cull.
- [ ] **Texture footprint is not bandwidth**: one texel per pixel, no cache, filter taps or anisotropy; vertex-shader
  textures not counted; atlas detection is by name (`atlas`, `sactx`).
- [ ] **Bandwidth numbers are upper bounds**: whole attachments, no framebuffer compression, no tile-level partial
  writes. Good for ranking and before/after, not absolute GB/s.
- [ ] **No cross-check against a hardware profiler.** Nothing here has been compared with Streamline / Snapdragon
  Profiler counters. Recommended before acting on a single number from the report.
- [ ] **Report thresholds are rules of thumb** (draw calls 500 / 1000, avoidable traffic 10 % / 25 %, hidden pixels
  10 % / 25 %). Percentages are only comparable inside their own area.

## P2 – speed and usability

- [ ] **`get_draw_coverage` is ~150 s for 839 draws** (95 % is `SetFrameEvent`, ~0.12 s/draw on the phone). Ideas:
  skip draws that cannot differ (same state and render target as the previous one), or cap per-marker sampling.
- [ ] **A snapshot takes ~3 min on 839 draws, `compare_captures` ~7 min plus two loads**, and `open_capture` waits
  60 s after each load (`RENDERDOC_MCP_COMPARE_SETTLE`). The wait was added for the random crashes; remove it once
  the P0 item is solved.
- [ ] **`frame_performance_report` returns many low-value actions** (e.g. one `wasted_store` per small temp buffer).
  Group by marker / target, and show the top few per area.
- [ ] **Tool descriptions are English, project docs are mixed Chinese / Japanese** (CLAUDE.md headings are Japanese).
  Pick one language for the docs.

## P3 – tooling and environment

- [ ] **`scripts/capture_android.py` only works for debuggable apps** (uses `run-as`). Non-debuggable builds need the
  layer packaged into the APK, or `<queries><package android:name="org.renderdoc.renderdoccmd.arm64"/></queries>`
  in the manifest (not tried). arm64 only.
- [ ] **Remote replay needs the phone's RenderDoc remote server started by hand** (`am start ... renderdoccmd remoteserver`)
  when qrenderdoc was killed; a stale "Remote side of network connection is busy" needs `am force-stop` first.
- [ ] **An elevated qrenderdoc started by the user cannot be closed from the agent session** and races for the IPC
  files. `RENDERDOC_MCP_IPC_DIR` works around it, but both ends must be configured with the same value.
- [ ] **The MCP server process in a running Claude Code session is the old code** until the client restarts; tools
  added later are invisible to it.
- [ ] **Repo hygiene**: the working tree is CRLF while the index is LF (`core.autocrlf=true`), which broke partial
  staging twice; add a `.gitattributes` (`* text=auto eol=lf` for `*.py`, `*.md`).
- [ ] **No automated test suite in the repo.** The offline tests used for these features live in a scratch directory
  (`%TEMP%/rdmcp_scratch/t_*.py`, `e2e.py`) and are not committed. Move the pure-function tests (`vk_passes`,
  `gl_state`, `draw_analysis`, `compare`, `texture_analysis`, `bandwidth_service`) into `tests/` and keep `e2e.py` as
  a documented manual check with its expected numbers.
