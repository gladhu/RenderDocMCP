# TODO

Known problems and limits, found while building and testing the performance-analysis tools
(GLES: Unity / Mali-G610, `8.057.001.108` capture; Vulkan: `test-Vulkan_02` capture, 59 draws, 2 render passes).
Each item says what is known, what is not, and how to tell it is done.

## P0 – stability

- [x] **qrenderdoc access-violates at random on the replay thread.** Root cause found: the custom RenderDoc 1.43
  build (`D:\Program Files\RenderDoc_AI_20260716`, Python 3.14, no PySide2) corrupts the Python heap in its
  `BlockInvoke` callback glue. Not caused by any of the call patterns blamed before.
  - Evidence: a request that only calls `ctx.Replay().BlockInvoke(lambda c: None)` and then runs GC crashes that
    build by the 1st–2nd round (remote and local replay, callable pinned or not, replay-thread state kept or not);
    the same request without `BlockInvoke` survives. Official RenderDoc 1.46 (Python 3.6, PySide2): 10/10 rounds
    clean. The same `renderdoc.pyd` in a standalone Python (real phone replay, foreign-thread callbacks, debug
    allocator) never crashes. Full-memory dumps show a double-freed 1-tuple in the tuple free list whose link
    pointer was decremented by one, i.e. a non-atomic `dec` on freed memory; the disassembly of the glue shows
    reference counts changed without the GIL (`std::function` copy, `ScopedFuncCall`).
  - Resolution: use the official build (documented in README / CLAUDE.md). Acceptance: 20 consecutive relaunch +
    `open_capture(use_remote=True)` (no settle wait) + full e2e runs on the phone (10 Vulkan, 10 GLES), 0 crashes.
  - Kept as hardening: the poll thread hands the UI thread one pinned callable instead of a new bound method per
    request; faulthandler output is per IPC directory (`fault.log`).
  - Open: the custom build's glue needs fixing in its own source (`qrenderdoc/Code/pyrenderdoc`: `ConvertFunc`,
    `ScopedFuncCall`, the `std::function` copy of `PyObjectRefCounter`, the `BlockInvoke` wrapper). Until then the
    extension is not reliable there.

- [x] **`stat_instancing_efficiency` / `get_draw_calls` still use the walk pattern that crashed.** Same root cause;
  the walks are fine on the official build.

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
- [x] Fixed in the review of 2026-10-29: `cycles_per_pixel` was diluted by draws whose shader did not compile;
  `diff_snapshots` raised when a shader had 0 pixels on one side; the snapshot / report only saw the first 50
  bandwidth issues and the 5 longest batchable runs (totals now come from the extension); `compare_captures` lost
  the first snapshot when the second capture failed; `open_capture` did not check that the requested file was the
  one loaded; `get_cached_message` could not page large non-list fields (shader sources); late-Z waste could exceed
  100 %; an empty `event_ids` list silently selected every draw.

## P2 – speed and usability

- [ ] **`get_draw_coverage` is ~150 s for 839 draws** (95 % is `SetFrameEvent`, ~0.12 s/draw on the phone). Ideas:
  skip draws that cannot differ (same state and render target as the previous one), or cap per-marker sampling.
  `estimate_shading_cost` / `estimate_geometry_cost` / `analyze_overdraw` now get coverage and shader sources from
  one replay pass instead of two.
  Measured 2026-10-30 (GLES capture, 15-17 draws per run, Mali-G610): per draw `SetFrameEvent` ~0.13-0.17 s, depth overlay
  ~0.06 s, two histograms ~0.025 s. **`SetFrameEvent(eid, False)` instead of `True` makes no difference** (4 runs each,
  within the phone's own +-15 % noise, identical per-draw results), so that flag is not the lever. The cost is the
  replay of the event itself. Vulkan has an exact shortcut for the PASSED count only: the `Samples Passed` counter
  (`get_gpu_counters`) equals the depth-overlay passed pixels and takes 0.2 s for the whole frame, but it cannot give
  the FAILED (occluded) count that overdraw / hidden-pixel analysis needs, so it cannot replace the overlay. Ideas left:
  sample draws per (shader, render target) and extrapolate (changes the meaning: estimate, not measurement), or run only
  the draws of the passes the question is about (`marker_filter` already allows this).
- [x] **Settle wait after `open_capture`** removed (default 0; `RENDERDOC_MCP_COMPARE_SETTLE` still overrides). It had
  been added for the crashes above. `open_capture` now waits for `capture_loading` to clear.
- [ ] **A snapshot takes ~3 min on 839 draws, `compare_captures` ~7 min plus two loads.**
- [x] **`frame_performance_report` returns many low-value actions.** Bandwidth issues keep the 3 heaviest per type;
  the rest of a type become one "N more ..." action (`actions_folded`).
- [x] **Docs language.** CLAUDE.md is Chinese now, like README; tool descriptions stay English.

## P3 – tooling and environment

- [ ] **`scripts/capture_android.py` only works for debuggable apps** (uses `run-as`). Non-debuggable builds need the
  layer packaged into the APK, or `<queries><package android:name="org.renderdoc.renderdoccmd.arm64"/></queries>`
  in the manifest (not tried). arm64 only.
- [ ] **Remote replay needs the phone's RenderDoc server of the same version.** The official 1.46 build installs its
  APK when it connects; on Android 11+ the first start opens the "all files access" page — grant it on the phone or
  with `adb shell appops set --uid org.renderdoc.renderdoccmd.arm64 MANAGE_EXTERNAL_STORAGE allow`. The custom 1.43
  build and 1.46 share the package name and replace each other. A stale "Remote side of network connection is
  busy" needs `am force-stop` first.
- [ ] **Seen twice: phone server drops the connection and the next `open_capture` leaves RenderDoc in a modal "Error opening capture" dialog.**
  The second time (2026-10-30) it happened while the machine was idle for a few minutes after short replay work (`ps` on the phone showed no renderdoc server any more; adb and RenderDoc were fine), so it is not only "after long replay work". `scripts/dev_cycle.py ensure <capture>` detects an unresponsive RenderDoc and restarts it. The original entry follows.
- [ ] **(original entry) phone server drops the connection on the next `open_capture` after long replay work.** After
  ~10 min of back-to-back full-frame replays (1.46, GLES capture), opening the Vulkan capture failed with
  "Error opening capture" / "Socket unexpectedly disconnected". RenderDoc then shows a modal error on the UI thread and
  the extension stops answering until qrenderdoc is restarted. A plain capture switch (GLES -> Vulkan -> GLES) does
  not reproduce it. Idea: have `open_capture` detect a dead remote first (`CurrentRemote().Ping()`) and reconnect.
- [ ] **The phone's replay speed varies a lot between sessions (up to ~4x), and a killed client leaves its request running.**
  Measured 2026-10-30 on the same GLES capture and the same untouched code: `diagnose_mobile_risks` 12.8 s earlier in the day,
  48-49 s later (checked on commits `72bd61b`, `e53c1a1` and HEAD alike, so it is not the code); `get_draw_coverage` over 839 draws
  172 s -> 542 s; `open_capture` 10 s -> 25-40 s. Force-stopping the phone server did not change it. Not explained (GPU clock /
  power state of the phone is the suspect). Two consequences: give long checks generous timeouts, and note that while a replay
  request runs RenderDoc's UI thread is blocked inside it, so Windows shows the window as "Not responding" - that is not a hang.
  A client that is killed (or `timeout`-ed) after its request was picked up does not cancel it: the next client's requests queue
  behind the leftover work, which looked like a hang once. Idea: have the extension skip requests whose client is gone
  (request file removed) and expose a `cancel` for the running one.
- [x] **Two MCP clients or two qrenderdoc instances racing for the IPC files.** Each request now has its own
  `request_<id>.json` / `response_<id>.json`, written to a temp name and renamed; the old single-slot protocol
  is still served. `RENDERDOC_MCP_IPC_DIR` still separates instances.
- [ ] **The MCP server process in a running Claude Code session is the old code** until the client restarts; tools
  added later are invisible to it (the extension still answers its old protocol).
- [x] **Repo hygiene**: `.gitattributes` keeps `*.py` / `*.md` / `*.json` / `*.toml` / `*.sh` LF in the working tree.
- [x] **Automated tests**: offline tests in `tests/` (`python -m unittest discover -s tests`, stdlib only);
  `scripts/e2e_check.py gles|vulkan` is the manual end-to-end check with the expected numbers of the two captures.
