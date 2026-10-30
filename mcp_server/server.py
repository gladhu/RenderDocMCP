"""
RenderDoc MCP Server
FastMCP 2.0 server providing access to RenderDoc capture data.
"""

import os
import time
from typing import Literal

from fastmcp import FastMCP

from .bridge.client import RenderDocBridge, RenderDocBridgeError
from .config import settings
from . import compare, draw_analysis, report, result_cache, texture_analysis
from .params import (
    LInt, OptInt, OptIntList, ReqInt, StageOpt, StageReq, StageVP, StageVPCOpt,
)

# Initialize FastMCP server
mcp = FastMCP(
    name="RenderDoc MCP Server",
)
# Results over RENDERDOC_MCP_MAX_RESPONSE_CHARS are cached and returned as a message_id
mcp.add_middleware(result_cache.ResultCacheMiddleware())

# RenderDoc bridge client
bridge = RenderDocBridge(host=settings.renderdoc_host, port=settings.renderdoc_port)


@mcp.tool
def get_capture_status() -> dict:
    """
    Check if a capture is currently loaded in RenderDoc.
    Returns the capture status and API type if loaded.
    """
    return bridge.call("get_capture_status")


@mcp.tool
def get_replay_context() -> dict:
    """
    Get the current replay context information from RenderDoc.

    Returns details about the replay device configuration:
    - remote_connected: Whether a remote device is connected for replay
    - remote_hostname: Hostname of the connected remote device
    - remote_friendly_name: Display name of the remote device
    - is_localhost: Whether the current replay target is localhost
    - capture_loaded: Whether a capture is currently loaded
    - capture_local: Whether the loaded capture file is local (only present if capture loaded)

    This is useful for checking which device will be used for replay before
    opening a capture, especially for mobile GPU captures that require
    remote replay on the original device.
    """
    return bridge.call("get_replay_context")


@mcp.tool
def get_draw_calls(
    include_children: bool = True,
    marker_filter: str | None = None,
    exclude_markers: list[str] | None = None,
    event_id_min: OptInt = None,
    event_id_max: OptInt = None,
    only_actions: bool = False,
    flags_filter: list[str] | None = None,
) -> dict:
    """
    Get the list of all draw calls and actions in the current capture.

    Args:
        include_children: Include child actions in the hierarchy (default: True)
        marker_filter: Only include actions under markers containing this string (partial match)
        exclude_markers: Exclude actions under markers containing these strings (list of partial matches)
        event_id_min: Only include actions with event_id >= this value
        event_id_max: Only include actions with event_id <= this value
        only_actions: If True, exclude marker actions (PushMarker/PopMarker/SetMarker)
        flags_filter: Only include actions with these flags (list of flag names, e.g. ["Drawcall", "Dispatch"])

    Returns a hierarchical tree of actions including markers, draw calls,
    dispatches, and other GPU events.
    """
    params: dict[str, object] = {"include_children": include_children}
    if marker_filter is not None:
        params["marker_filter"] = marker_filter
    if exclude_markers is not None:
        params["exclude_markers"] = exclude_markers
    if event_id_min is not None:
        params["event_id_min"] = event_id_min
    if event_id_max is not None:
        params["event_id_max"] = event_id_max
    if only_actions:
        params["only_actions"] = only_actions
    if flags_filter is not None:
        params["flags_filter"] = flags_filter
    return bridge.call("get_draw_calls", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_frame_summary() -> dict:
    """
    Get a summary of the current capture frame.

    Returns statistics about the frame including:
    - API type (D3D11, D3D12, Vulkan, etc.)
    - Total action count
    - Statistics: draw calls, dispatches, clears, copies, presents, markers
    - Top-level markers with event IDs and child counts
    - Resource counts: textures, buffers
    """
    return bridge.call("get_frame_summary")


@mcp.tool
def find_draws_by_shader(
    shader_name: str,
    stage: StageOpt = None,
) -> dict:
    """
    Find all draw calls using a shader with the given name (partial match).

    Args:
        shader_name: Partial name to search for in shader names or entry points
        stage: Optional shader stage to search (if not specified, searches all stages)

    Returns a list of matching draw calls with event IDs and match reasons.
    """
    params: dict[str, object] = {"shader_name": shader_name}
    if stage is not None:
        params["stage"] = stage
    return bridge.call("find_draws_by_shader", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def find_draws_by_texture(texture_name: str) -> dict:
    """
    Find all draw calls using a texture with the given name (partial match).

    Args:
        texture_name: Partial name to search for in texture resource names

    Returns a list of matching draw calls with event IDs and match reasons.
    Searches SRVs, UAVs, and render targets.
    """
    return bridge.call("find_draws_by_texture", {"texture_name": texture_name}, timeout=_LONG_TIMEOUT)


@mcp.tool
def find_draws_by_resource(resource_id: str) -> dict:
    """
    Find all draw calls using a specific resource ID (exact match).

    Args:
        resource_id: Resource ID to search for (e.g. "ResourceId::12345" or "12345")

    Returns a list of matching draw calls with event IDs and match reasons.
    Searches shaders, SRVs, UAVs, render targets, and depth targets.
    """
    return bridge.call("find_draws_by_resource", {"resource_id": resource_id}, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_draw_call_details(event_id: ReqInt) -> dict:
    """
    Get detailed information about a specific draw call.

    Args:
        event_id: The event ID of the draw call to inspect

    Includes vertex/index counts, resource outputs, and other metadata.
    """
    return bridge.call("get_draw_call_details", {"event_id": event_id})


@mcp.tool
def get_action_timings(
    event_ids: OptIntList = None,
    marker_filter: str | None = None,
    exclude_markers: list[str] | None = None,
) -> dict:
    """
    Get GPU timing information for actions (draw calls, dispatches, etc.).

    Args:
        event_ids: Optional list of specific event IDs to get timings for.
                   If not specified, returns timings for all actions.
        marker_filter: Only include actions under markers containing this string (partial match).
        exclude_markers: Exclude actions under markers containing these strings.

    Returns timing data including:
    - available: Whether GPU timing counters are supported
    - unit: Time unit (typically "seconds")
    - timings: List of {event_id, name, duration_seconds, duration_ms}
    - total_duration_ms: Sum of all durations
    - count: Number of timing entries

    Note: GPU timing counters may not be available on all hardware/drivers.
    On tile-based mobile GPUs (Mali/Adreno/PowerVR) per-draw timer queries force
    tile flushes, so individual durations are inflated and do not add up to the
    frame time; use estimate_shading_cost / get_draw_coverage for those captures.
    """
    params: dict[str, object] = {}
    if event_ids is not None:
        params["event_ids"] = event_ids
    if marker_filter is not None:
        params["marker_filter"] = marker_filter
    if exclude_markers is not None:
        params["exclude_markers"] = exclude_markers
    return bridge.call("get_action_timings", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_shader_info(
    event_id: ReqInt,
    stage: StageReq,
) -> dict:
    """
    Get shader information for a specific stage at a given event.

    Args:
        event_id: The event ID to inspect the shader at
        stage: The shader stage (vertex, hull, domain, geometry, pixel, compute)

    Returns shader disassembly, constant buffer values, and resource bindings.
    """
    return bridge.call("get_shader_info", {"event_id": event_id, "stage": stage})


@mcp.tool
def get_buffer_contents(
    resource_id: str,
    offset: LInt = 0,
    length: LInt = 0,
) -> dict:
    """
    Read the contents of a buffer resource.

    Args:
        resource_id: The resource ID of the buffer to read
        offset: Byte offset to start reading from (default: 0)
        length: Number of bytes to read, 0 for entire buffer (default: 0)

    Returns buffer data as base64-encoded bytes along with metadata.
    """
    return bridge.call(
        "get_buffer_contents",
        {"resource_id": resource_id, "offset": offset, "length": length},
    )


@mcp.tool
def get_texture_info(resource_id: str) -> dict:
    """
    Get metadata about a texture resource.

    Args:
        resource_id: The resource ID of the texture

    Includes dimensions, format, mip levels, and other properties.
    """
    return bridge.call("get_texture_info", {"resource_id": resource_id})


@mcp.tool
def get_texture_data(
    resource_id: str,
    mip: LInt = 0,
    slice: LInt = 0,
    sample: LInt = 0,
    depth_slice: OptInt = None,
) -> dict:
    """
    Read the pixel data of a texture resource.

    Args:
        resource_id: The resource ID of the texture to read
        mip: Mip level to retrieve (default: 0)
        slice: Array slice or cube face index (default: 0)
               For cube maps: 0=X+, 1=X-, 2=Y+, 3=Y-, 4=Z+, 5=Z-
        sample: MSAA sample index (default: 0)
        depth_slice: For 3D textures only, extract a specific depth slice (default: None = full volume)
                     When specified, returns only the 2D slice at that depth index

    Returns texture pixel data as base64-encoded bytes along with metadata
    including dimensions at the requested mip level and format information.
    """
    params = {"resource_id": resource_id, "mip": mip, "slice": slice, "sample": sample}
    if depth_slice is not None:
        params["depth_slice"] = depth_slice
    return bridge.call("get_texture_data", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_pipeline_state(event_id: ReqInt) -> dict:
    """
    Get the full graphics pipeline state at a specific event.

    Args:
        event_id: The event ID to get pipeline state at

    Returns detailed pipeline state including:
    - Bound shaders with entry points for each stage
    - Shader resources (SRVs): textures and buffers with dimensions, format, slot, name
    - UAVs (RWTextures/RWBuffers): resource details with dimensions and format
    - Samplers: addressing modes, filter settings, LOD parameters
    - Constant buffers: slot, size, variable count
    - Render targets and depth target
    - Viewports and input assembly state
    """
    return bridge.call("get_pipeline_state", {"event_id": event_id})


@mcp.tool
def read_constant_buffer(
    event_id: ReqInt,
    stage: StageReq,
    slot: int | str | None = None,
    array_index: LInt = 0,
    filter: str | None = None,
) -> dict:
    """
    Read one constant buffer (uniform block) of a shader stage at an event, as
    named variables (vec4 / mat4 / int ...). Much lighter than get_pipeline_state,
    which dumps every buffer of every stage.

    Args:
        event_id: Draw call event id
        stage: vertex / pixel / compute (vs / ps / cs also accepted)
        slot: Index into the shader's constant blocks, or a cbuffer name (exact,
            substring, or "cb5" style). NOT the API bind point: on GLES captures
            every block reports bind point 0, so use the index from the list.
            Leave empty to list the stage's blocks (index, name, size) first.
        array_index: Element when the block is an array of buffers (default 0)
        filter: Case-insensitive substring of the variable name; only matching
            variables are returned (e.g. "rim", "_ST", "unity_SH"). UnityPerMaterial
            alone has dozens of variables, so filtering keeps the output short.

    Returns the bound resource, byte range and per-variable values; a block with
    nothing bound reports status "No Resource".
    """
    params: dict[str, object] = {"event_id": event_id, "stage": stage, "array_index": array_index}
    if slot is not None and not (isinstance(slot, str) and not slot.strip()):
        params["slot"] = slot
    result = bridge.call("read_constant_buffer", params, timeout=_LONG_TIMEOUT)
    needle = filter.strip().lower() if filter else ""
    if needle and "variables" in result:
        total = len(result["variables"])
        result["variables"] = [v for v in result["variables"] if needle in v["name"].lower()]
        result["filter"] = {"text": filter, "matched": len(result["variables"]), "total": total}
    return result


@mcp.tool
def list_captures(directory: str) -> dict:
    """
    List all RenderDoc capture files (.rdc) in the specified directory.

    Args:
        directory: The directory path to search for capture files

    Returns a list of capture files with their metadata including:
    - filename: The capture file name
    - path: Full path to the file
    - size_bytes: File size in bytes
    - modified_time: Last modified timestamp (ISO format)
    """
    return bridge.call("list_captures", {"directory": directory})


_OPEN_CAPTURE_WAIT = 180.0  # seconds to wait for LoadCapture to finish
# Extra wait after a load before the first replay call. The random crashes it was added for were heap
# corruption in the custom 1.43 build's BlockInvoke (see CLAUDE.md), not a load race, so it is 0 by default.
_OPEN_CAPTURE_SETTLE = _COMPARE_SETTLE = float(os.environ.get("RENDERDOC_MCP_COMPARE_SETTLE", "0"))


@mcp.tool
def open_capture(
    capture_path: str,
    use_remote: bool | None = None,
) -> dict:
    """
    Open a RenderDoc capture file (.rdc).

    If a remote device is already connected in RenderDoc GUI, the capture
    will automatically be replayed on the remote device instead of locally.
    This is essential for captures from mobile devices (e.g. Android/Vulkan)
    that require specific GPU extensions not available on the local PC.

    Args:
        capture_path: Full path to the capture file to open
        use_remote: If True, force remote replay; if False, force local replay;
                    if not specified, auto-detect (use remote if connected, else local)

    Returns success status and information about the opened capture,
    including which device (local or remote) is used for replay.
    Note: This will close any currently open capture.
    """
    return _open_capture(capture_path, use_remote, _OPEN_CAPTURE_SETTLE)


def _open_capture(capture_path: str, use_remote: bool | None, settle: float) -> dict:
    # wait=False: the extension runs on RenderDoc's UI thread, which LoadCapture needs
    # in order to finish, so the extension must not spin-wait for it. Poll from here.
    params: dict[str, object] = {"capture_path": capture_path, "wait": False}
    if use_remote is not None:
        params["use_remote"] = use_remote
    result = bridge.call("open_capture", params, timeout=120.0)

    deadline = time.time() + _OPEN_CAPTURE_WAIT
    while time.time() < deadline:
        time.sleep(1.0)
        # get_replay_context only reads UI state; get_capture_status would invoke the
        # replay thread while the capture is still being brought up.
        ctx = bridge.call("get_replay_context")
        if ctx.get("capture_loaded") and not ctx.get("capture_loading"):
            break
    else:
        raise RenderDocBridgeError(
            "Capture loading timed out after %d seconds" % _OPEN_CAPTURE_WAIT
        )

    time.sleep(settle)
    status = bridge.call("get_capture_status")
    # the loaded file must be the requested one, not the previous capture that was still open
    loaded = status.get("filename") or ""
    if loaded and os.path.normcase(os.path.abspath(loaded)) != os.path.normcase(os.path.abspath(capture_path)):
        raise RenderDocBridgeError("Expected %s to be loaded, but RenderDoc has %s" % (capture_path, loaded))

    # cached large results (get_cached_message) belong to the previous capture
    result_cache.clear()
    result.pop("loading", None)
    result["api"] = status.get("api")
    if status.get("vendor"):
        result["vendor"] = status["vendor"]
    return result


# ==================== Performance analysis ====================

# Long-running analysis calls replay many events; give them more time than the default 120 s
_LONG_TIMEOUT = 1800.0


def _selection(event_ids, marker_filter, event_id_min, event_id_max) -> dict[str, object]:
    params: dict[str, object] = {}
    if event_ids is not None:
        params["event_ids"] = event_ids
    if marker_filter is not None:
        params["marker_filter"] = marker_filter
    if event_id_min is not None:
        params["event_id_min"] = event_id_min
    if event_id_max is not None:
        params["event_id_max"] = event_id_max
    return params


@mcp.tool
def get_draw_coverage(
    event_ids: OptIntList = None,
    marker_filter: str | None = None,
    event_id_min: OptInt = None,
    event_id_max: OptInt = None,
    summary_only: bool = False,
    with_state: bool = False,
) -> dict:
    """
    Measure how many pixels each draw call actually shades, using RenderDoc's
    depth-test overlay (counted with a GPU histogram, no image transfer).

    - passed: pixels that passed the depth test (≈ fragment shader invocations)
    - failed: pixels covered by the draw but rejected by depth (occluded)

    Runs on the replay device: with remote replay (open_capture(use_remote=True))
    the numbers come from the phone GPU, which avoids desktop-replay artifacts
    of mobile GLES captures. Note the overlay ignores shader discard, so
    alpha-tested draws may be slightly over-counted.

    Select draws with event_ids, marker_filter (substring of any ancestor
    marker, e.g. "DrawOpaqueObjects") and/or an event id range.

    Each draw also reports num_indices, num_instances, topology and rt_size; with_state=True
    adds blend_enabled / depth_test / depth_write (one more state read per draw).
    """
    params = _selection(event_ids, marker_filter, event_id_min, event_id_max)
    if with_state:
        params["with_state"] = True
    data = bridge.call("get_draw_coverage", params, timeout=_LONG_TIMEOUT)
    if summary_only:
        data = {k: v for k, v in data.items() if k != "draws"}
    return data


@mcp.tool
def get_shader_usage(
    marker_filter: str | None = None,
    event_id_min: OptInt = None,
    event_id_max: OptInt = None,
) -> dict:
    """
    Group draw calls by pixel shader: draw count, primitive count, sample
    event ids and the most common bound textures (to identify the material).
    """
    return bridge.call(
        "get_shader_usage",
        _selection(None, marker_filter, event_id_min, event_id_max),
        timeout=_LONG_TIMEOUT,
    )


@mcp.tool
def get_shader_source(
    event_id: ReqInt,
    stage: StageVP = "pixel",
    fold_uniforms: bool = False,
    overrides: dict[str, str] | None = None,
    output_path: str | None = None,
) -> dict:
    """
    Get the GLSL source of a shader as captured (OpenGL / GLES captures).

    Args:
        event_id: Draw call event id
        stage: "pixel" or "vertex"
        fold_uniforms: Inject '#define <uniform> <actual value>' for every
            UnityPerMaterial scalar/vector before main(), so the source reflects
            the branch path this material really takes
        overrides: Extra {name: GLSL literal} defines, e.g. {"_RainbowOn": "1.0"}
        output_path: If set, write the source to this file and omit it from the result
    """
    params: dict[str, object] = {
        "event_ids": [event_id], "stage": stage, "fold_uniforms": fold_uniforms,
    }
    if overrides:
        params["overrides"] = overrides
    data = bridge.call("get_shader_sources", params, timeout=_LONG_TIMEOUT)
    if not data["draws"]:
        raise RenderDocBridgeError("No shader found at event %d" % event_id)
    draw = data["draws"][0]
    if "error" in draw:
        raise RenderDocBridgeError(draw["error"])
    source = data["sources"][draw["source_id"]]
    result: dict[str, object] = {
        "event_id": event_id, "shader": draw["shader"], "stage": stage,
        "folded_uniforms": draw["folded_uniforms"], "textures": draw["textures"],
    }
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(source)
        result["output_path"] = output_path
    else:
        result["source"] = source
    return result


_COMPILER_DEFAULT_GPU = {"mali": "Mali-G610", "adreno": "a650"}


def _compiler(compiler: str):
    """(module, default gpu, report key, version) for the chosen offline compiler"""
    if compiler == "mali":
        from . import malioc
        return malioc, _COMPILER_DEFAULT_GPU["mali"], "malioc", malioc.malioc_version
    if compiler == "adreno":
        from . import aoc
        return aoc, _COMPILER_DEFAULT_GPU["adreno"], "aoc", aoc.aoc_version
    raise ValueError("compiler must be 'mali' or 'adreno'")


def _report_cost(report: dict | None) -> float | None:
    """Per-pixel cost of one report: malioc bound cycles, or aoc instruction count"""
    if not report or "error" in report:
        return None
    return report.get("bound_cycles", report.get("cost"))


def _shader_cost(selection: dict[str, object], stage: str, compiler: str, gpu: str | None,
                 fold_uniforms: bool, overrides: dict[str, str] | None) -> tuple[dict, dict]:
    params = dict(selection, stage=stage, fold_uniforms=fold_uniforms)
    if overrides:
        params["overrides"] = overrides
    data = bridge.call("get_shader_sources", params, timeout=_LONG_TIMEOUT)
    return data, _compile_sources(data, stage, compiler, gpu)


def _coverage_and_cost(selection: dict[str, object], stage: str, compiler: str, gpu: str | None,
                       fold_uniforms: bool, **coverage_options) -> tuple[dict, dict, dict]:
    """Pixel coverage and one stage's shader sources from ONE replay pass, then the offline-compiler cost.

    A SetFrameEvent per draw dominates both calls, so a separate get_shader_sources would double the time.
    Returns (coverage, sources of that stage, compiler reports by source id).
    """
    params = dict(selection, with_sources={"stages": [stage], "fold_uniforms": fold_uniforms}, **coverage_options)
    coverage = bridge.call("get_draw_coverage", params, timeout=_LONG_TIMEOUT)
    data = coverage["sources"][stage]
    return coverage, data, _compile_sources(data, stage, compiler, gpu)


def _compile_sources(data: dict, stage: str, compiler: str, gpu: str | None) -> dict:
    """Offline-compile the sources of one get_shader_sources result"""
    module, default_gpu, _, _ = _compiler(compiler)
    return module.analyze_many(data["sources"], stage, gpu or default_gpu, language=data.get("language", "glsl"))


@mcp.tool
def analyze_shader_cost(
    event_ids: OptIntList = None,
    marker_filter: str | None = None,
    event_id_min: OptInt = None,
    event_id_max: OptInt = None,
    stage: StageVP = "pixel",
    compiler: Literal["mali", "adreno"] = "mali",
    gpu: str | None = None,
    fold_uniforms: bool = True,
    overrides: dict[str, str] | None = None,
) -> dict:
    """
    Static shader cost with a mobile offline compiler for the selected draws:
    Mali (malioc) or Adreno (aoc).

    With fold_uniforms (default), each draw's UnityPerMaterial values are folded
    into the source, so the reported cycles are for the path that material
    actually executes (e.g. disabled effect branches are dropped). Identical
    folded sources are compiled once.
    Caveat: folding also lets the compiler constant-fold plain math such as
    lerp(x, y, 0) that the real GPU still executes. It is accurate for code
    behind uniform branches, but underestimates branch-free shaders; use
    fold_uniforms=False for the cost the GPU actually pays there.

    Returns per unique source, plus which draws use it:
    - mali: bound cycles per pixel/vertex, per-pipeline cycles (arith/varying/
      texture...), work registers, thread occupancy, fp16 ratio, late-ZS / coverage
      flags (discard). 'gpu' is the core name, default "Mali-G610" (e.g. "Mali-G710").
      Requires malioc (Arm Performance Studio) on PATH or MALIOC_PATH.
    - adreno: instruction counts per class (alu32/alu16/texture/flow control...),
      registers (full/half), fiber occupancy, scratch spilling. aoc has no cycle model,
      so 'cost' is the total instruction count (comparable only between Adreno
      shaders). 'gpu' is the architecture, default "a650" (e.g. "a730", "a740").
      Requires aoc (Qualcomm Adreno SDK) on PATH or AOC_PATH.
    Pick the compiler that matches the capture device GPU (see get_replay_context).

    Vulkan captures: the shaders are SPIR-V binaries, compiled directly by malioc (compiler="mali"
    only). There is no GLSL to fold, so fold_uniforms / overrides have no effect: the cost is that of
    the whole shader, including material branches a given draw never takes.
    """
    _, default_gpu, report_key, version = _compiler(compiler)
    gpu = gpu or default_gpu
    data, reports = _shader_cost(
        _selection(event_ids, marker_filter, event_id_min, event_id_max),
        stage, compiler, gpu, fold_uniforms, overrides,
    )
    by_source: dict[str, dict] = {}
    for draw in data["draws"]:
        sid = draw.get("source_id")
        if sid is None:
            continue
        entry = by_source.setdefault(sid, {
            "source_id": sid, "shader": draw["shader"], "draw_count": 0,
            "sample_event_ids": [], "textures": draw["textures"], report_key: reports.get(sid),
        })
        entry["draw_count"] += 1
        if len(entry["sample_event_ids"]) < 5:
            entry["sample_event_ids"].append(draw["event_id"])
    sources = sorted(by_source.values(), key=lambda e: -e["draw_count"])
    return {
        "compiler": compiler,
        "gpu": gpu,
        "%s_version" % report_key: version(),
        "stage": stage,
        "fold_uniforms": fold_uniforms,
        "draw_count": len(data["draws"]),
        "unique_sources": len(sources),
        "sources": sources,
        "errors": [d for d in data["draws"] if "error" in d],
    }


@mcp.tool
def estimate_shading_cost(
    marker_filter: str | None = None,
    event_id_min: OptInt = None,
    event_id_max: OptInt = None,
    event_ids: OptIntList = None,
    compiler: Literal["mali", "adreno"] = "mali",
    gpu: str | None = None,
    fold_uniforms: bool = True,
    top_n: LInt = 30,
) -> dict:
    """
    Estimate where fragment shading cost goes in a frame:
    pixels shaded per draw (depth-test overlay, measured on the replay device)
    × offline-compiler cost per pixel of that draw's material-folded shader
    (compiler="mali": malioc bound cycles; compiler="adreno": aoc instruction count,
    see analyze_shader_cost for 'gpu' and the Adreno cost caveat).

    Results are grouped by pixel shader with the dominant textures to identify
    the material. Use it to rank shaders and to compare before/after captures
    (cycles/pixel is view-independent; total cycles depends on the view).
    Run it with remote replay for mobile captures.
    """
    _, default_gpu, _, _ = _compiler(compiler)
    gpu = gpu or default_gpu
    selection = _selection(event_ids, marker_filter, event_id_min, event_id_max)
    coverage, data, reports = _coverage_and_cost(selection, "pixel", compiler, gpu, fold_uniforms)
    return _shading_result(coverage, data, reports, compiler, gpu, top_n)


def _shading_result(coverage: dict, data: dict, reports: dict, compiler: str, gpu: str, top_n: int) -> dict:
    _, _, _, version = _compiler(compiler)
    source_of = {d["event_id"]: d.get("source_id") for d in data["draws"]}
    hash_of = {d["event_id"]: d.get("shader_hash") for d in data["draws"]}

    groups: dict[str, dict] = {}
    total_cycles = 0.0
    for draw in coverage["draws"]:
        sid = source_of.get(draw["event_id"])
        report = reports.get(sid) if sid else None
        cyc = _report_cost(report)
        g = groups.setdefault(draw["pixel_shader"], {
            "pixel_shader": draw["pixel_shader"], "draw_count": 0, "pixels": 0, "costed_pixels": 0, "occluded_pixels": 0,
            "cycles": 0.0, "textures": {}, "sample_event_ids": [], "late_zs_draws": 0,
            "work_registers": set(), "unknown_cost_draws": 0, "shader_hash": hash_of.get(draw["event_id"]),
        })
        g["draw_count"] += 1
        g["pixels"] += draw["passed"]
        g["occluded_pixels"] += draw["failed"]
        if cyc is None:
            g["unknown_cost_draws"] += 1
        else:
            g["costed_pixels"] += draw["passed"]
            g["cycles"] += draw["passed"] * cyc
            total_cycles += draw["passed"] * cyc
            if report.get("uses_late_zs_update") or report.get("modifies_coverage"):  # mali only
                g["late_zs_draws"] += 1
            regs = report.get("work_registers", report.get("registers"))
            if regs is not None:
                g["work_registers"].add(regs)
        for t in draw["textures"]:
            g["textures"][t] = g["textures"].get(t, 0) + 1
        if len(g["sample_event_ids"]) < 5:
            g["sample_event_ids"].append(draw["event_id"])

    rows = []
    for g in groups.values():
        # only pixels of draws whose cost is known: draws malioc could not compile would dilute it
        g["cycles_per_pixel"] = round(g["cycles"] / g["costed_pixels"], 3) if g["costed_pixels"] else None
        g["share_of_cycles"] = round(g["cycles"] / total_cycles, 4) if total_cycles else None
        g["cycles"] = round(g["cycles"])
        g["textures"] = [n for n, _ in sorted(g["textures"].items(), key=lambda kv: -kv[1])[:4]]
        g["work_registers"] = sorted(g["work_registers"])
        rows.append(g)
    rows.sort(key=lambda g: -g["cycles"])
    unit = "malioc bound cycles" if compiler == "mali" else "aoc instructions"
    return {
        "compiler": compiler,
        "gpu": gpu,
        "%s_version" % ("malioc" if compiler == "mali" else "aoc"): version(),
        "draw_count": coverage["draw_count"],
        "total_pixels": coverage["total_passed"],
        "total_occluded_pixels": coverage["total_failed"],
        "total_cycles": round(total_cycles),
        "note": "cycles = sum(pixels_passed x %s); relative ranking, not wall time" % unit,
        "shaders": rows[:top_n],
    }


@mcp.tool
def estimate_geometry_cost(
    marker_filter: str | None = None,
    event_id_min: OptInt = None,
    event_id_max: OptInt = None,
    event_ids: OptIntList = None,
    compiler: Literal["mali", "adreno"] = "mali",
    gpu: str | None = None,
    fold_uniforms: bool = True,
    top_n: LInt = 10,
) -> dict:
    """
    Where the vertex / triangle budget goes: vertices and triangles per marker (pass), per vertex
    shader and per draw, with the offline-compiler vertex cost (cycles per vertex x vertices).

    Findings:
    - vertex_heavy_pass: one marker submits >= 20% of all vertices (shadow passes repeat casters
      per cascade, so they usually show up here)
    - micro_triangles: a large share of triangles rasterise to fewer than 4 pixels each, which
      wastes quad lanes (needs LODs / impostors). Backface-culled triangles also lower the figure.
    Vertex counts are index counts (upper bound); vertex cycles are relative and not comparable
    with fragment cycles. Needs remote replay for mobile captures (pixel coverage per draw).
    """
    selection = _selection(event_ids, marker_filter, event_id_min, event_id_max)
    coverage, data, reports = _coverage_and_cost(selection, "vertex", compiler, gpu, fold_uniforms)
    return _geometry_result(coverage, data, reports, compiler, gpu, int(top_n))


def _geometry_result(coverage: dict, data: dict, reports: dict, compiler: str, gpu: str | None, top_n: int) -> dict:
    vs_info = {}
    for d in data["draws"]:
        sid = d.get("source_id")
        report_ = reports.get(sid) if sid else None
        vs_info[d["event_id"]] = {
            "cost": _report_cost(report_) if report_ else None,
            "hash": d.get("shader_hash"), "shader": d.get("shader"),
        }
    result = draw_analysis.geometry_cost(coverage["draws"], vs_info, top_n)
    _, default_gpu, _, _ = _compiler(compiler)
    result.update({"compiler": compiler, "gpu": gpu or default_gpu})
    return result


@mcp.tool
def estimate_texture_bandwidth(
    marker_filter: str | None = None,
    event_id_min: OptInt = None,
    event_id_max: OptInt = None,
    event_ids: OptIntList = None,
    top_n: LInt = 15,
) -> dict:
    """
    Texture sampling footprint of the frame: for every texture, an upper bound of the memory it can
    pull per frame = min(texture size, pixels that sample it x bytes per texel), summed over draws.

    Findings:
    - low_utilization: a big non-atlas texture of which the frame samples at most 25% of the texels
      (resolution higher than needed; ignore atlases drawn in small parts)
    - uncompressed_texture: large RGBA8 / RGBA16F ... colour texture, with the saving if ASTC 6x6
    - heavy_render_target_read: a full-size render target sampled as a texture (store + fetch)
    Also lists the heaviest textures, footprint per pass, and single-mip colour textures (info only).

    This is not measured bandwidth: caches, filter taps and anisotropy are not modelled, one texel
    per pixel is assumed. Use it to rank textures and compare before / after. Needs per-draw pixel
    coverage, so mobile captures need remote replay.
    """
    selection = _selection(event_ids, marker_filter, event_id_min, event_id_max)
    coverage = bridge.call("get_draw_coverage", dict(selection, with_textures=True), timeout=_LONG_TIMEOUT)
    textures = bridge.call("list_frame_textures", {"sort_by": "size", "top_n": 100000}, timeout=_LONG_TIMEOUT)
    return texture_analysis.texture_footprint(textures["textures"], coverage["draws"], int(top_n))


@mcp.tool
def analyze_overdraw(
    marker_filter: str | None = None,
    event_id_min: OptInt = None,
    event_id_max: OptInt = None,
    event_ids: OptIntList = None,
    compiler: Literal["mali", "adreno"] = "mali",
    gpu: str | None = None,
    fold_uniforms: bool = True,
    top_n: LInt = 10,
) -> dict:
    """
    Overdraw per render target, split by how each draw is rendered: opaque, late-Z opaque
    (pixel shader discards / writes depth, so early-Z cannot reject it) and transparent (blend on).

    Findings:
    - opaque_overdraw: many opaque pixels are drawn and then hidden (needs front-to-back sort / pre-Z)
    - late_z_waste: hidden pixels of late-Z draws still run the shader; reports the wasted cycles
    - transparent_layers: average blended layers per target pixel
    - draw_order: opaque draws issued after a transparent draw in the same target
    Uses the depth-test overlay on the replay device (use remote replay for mobile captures) and the
    offline compiler for the late-Z flags (Mali only; Adreno draws count as opaque).
    """
    selection = _selection(event_ids, marker_filter, event_id_min, event_id_max)
    coverage, data, reports = _coverage_and_cost(selection, "pixel", compiler, gpu, fold_uniforms, with_state=True)
    return _overdraw_result(coverage, data, reports, compiler, gpu, int(top_n))


def _overdraw_result(coverage: dict, data: dict, reports: dict, compiler: str, gpu: str | None, top_n: int) -> dict:
    ps_info = {}
    for d in data["draws"]:
        sid = d.get("source_id")
        report_ = reports.get(sid) if sid else None
        late_z = None
        if report_ and ("uses_late_zs_update" in report_ or "modifies_coverage" in report_):
            late_z = bool(report_.get("uses_late_zs_update") or report_.get("modifies_coverage"))
        ps_info[d["event_id"]] = {"cost": _report_cost(report_) if report_ else None, "late_z": late_z}
    result = draw_analysis.overdraw(coverage["draws"], ps_info, top_n)
    _, default_gpu, _, _ = _compiler(compiler)
    result.update({"compiler": compiler, "gpu": gpu or default_gpu})
    return result


@mcp.tool
def save_texture(
    output_path: str,
    event_id: OptInt = None,
    resource_id: str | None = None,
    target: str = "color0",
    file_type: Literal["png", "jpg", "bmp", "tga", "hdr", "exr", "dds"] = "png",
) -> dict:
    """
    Save a texture to a local image file (works with remote replay too).

    Either pass resource_id, or event_id with target ("color0", "color1", ...,
    or "depth") to save that event's render target as it is after the event.
    Useful to eyeball the replayed frame before trusting coverage numbers.
    """
    params: dict[str, object] = {"output_path": output_path, "target": target, "file_type": file_type}
    if event_id is not None:
        params["event_id"] = event_id
    if resource_id is not None:
        params["resource_id"] = resource_id
    return bridge.call("save_texture", params, timeout=300.0)


@mcp.tool
def pick_pixel(
    x: ReqInt,
    y: ReqInt,
    event_id: OptInt = None,
    resource_id: str | None = None,
    target: str = "color0",
) -> dict:
    """
    Read one pixel (color or depth) of a texture or of an event's render target
    ("color0".., "depth") after that event.
    """
    params: dict[str, object] = {"x": x, "y": y, "target": target}
    if event_id is not None:
        params["event_id"] = event_id
    if resource_id is not None:
        params["resource_id"] = resource_id
    return bridge.call("pick_pixel", params)


@mcp.tool
def diagnose_mobile_risks(
    check_categories: list[str] | None = None,
    severity_filter: Literal["all", "medium", "high"] = "all",
) -> dict:
    """
    Heuristic scan for mobile / tile-based GPU risks.

    Categories: precision (R11G11B10, D16), performance (too many fullscreen
    clears, discard), compatibility (textures > 4096), gpu_specific (Adreno /
    Mali / PowerVR quirks). Returns a priority-sorted list with a suggested fix.
    """
    params: dict[str, object] = {"severity_filter": severity_filter}
    if check_categories is not None:
        params["check_categories"] = check_categories
    return bridge.call("diagnose_mobile_risks", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_renderpass_list(
    sort_by: Literal["index", "draw_count", "event_id"] = "index",
) -> dict:
    """
    Enumerate render passes in the current capture.

    Resolution order: PassBoundary+BeginPass flags, then marker groups that
    contain draws, then a synthetic split on color/depth target changes.
    Each pass includes event range, draw count, and color/depth attachments.
    """
    return bridge.call("get_renderpass_list", {"sort_by": sort_by}, timeout=_LONG_TIMEOUT)


@mcp.tool
def analyze_pass_bandwidth(
    top_n: LInt = 30,
    sort_by: Literal["bytes", "index"] = "bytes",
) -> dict:
    """
    Attachment load/store traffic per tile pass (Mali / Adreno / any TBDR GPU).

    A tile GPU loads a framebuffer's attachments from memory when a pass starts
    (unless they are cleared / invalidated first) and stores them when it ends
    (unless invalidated). This is the bandwidth cost of the pass, independent of
    shader cost. OpenGL ES: inferred from the recorded calls (glBindFramebuffer, glClear*,
    glInvalidateFramebuffer, glBlitFramebuffer/glReadPixels, ...). Vulkan: read from the declared
    loadOp / storeOp of each render pass (vkCmdBeginRenderPass, and vkCmdBeginRendering for dynamic
    rendering, which has not been seen in a real capture yet); MSAA resolve targets are counted.

    Returns total traffic (load / store / wasted store MB, GB/s at 60 fps), a
    severity-sorted issue list (at most max(50, top_n) entries; issue_count, issues_by_type and
    avoidable_mb cover all of them) and the heaviest passes with per-attachment
    decisions. Issue types:
    - wasted_store: written back to memory but never read again (depth is the usual one)
    - load_without_clear: loaded at pass start although nothing wrote it this frame
    - render_target_reopened: same target rendered, left, and rendered again (extra load+store)
    - flush_by_readback: blit / copy / readpixels on the active target forces a tile flush
    - msaa_stored: multisampled buffer kept in memory instead of resolved in-tile
    - glFinish: pipeline stall
    Numbers are an upper bound (whole attachments, no framebuffer compression), good for
    ranking passes and for before/after comparison, not for absolute GB/s.

    Args:
        top_n: how many passes to return in detail
        sort_by: "bytes" (load+store, heaviest first) or "index" (frame order)
    """
    return bridge.call(
        "analyze_pass_bandwidth", {"top_n": top_n, "sort_by": sort_by}, timeout=_LONG_TIMEOUT
    )


@mcp.tool
def get_pass_detail(pass_index: ReqInt) -> dict:
    """
    List draw calls and attachments of one pass from get_renderpass_list.
    """
    return bridge.call("get_pass_detail", {"pass_index": pass_index}, timeout=_LONG_TIMEOUT)


@mcp.tool
def list_frame_textures(
    sort_by: Literal["size", "usage", "name"] = "size",
    top_n: LInt = 50,
    filter_fmt: str | None = None,
    min_width: OptInt = None,
    rt_only: bool = False,
) -> dict:
    """
    Frame-level texture inventory: format, resolution, estimated size,
    read/write/clear counts, and whether it is used as a render target.
    """
    params: dict[str, object] = {"sort_by": sort_by, "top_n": top_n, "rt_only": rt_only}
    if filter_fmt is not None:
        params["filter_fmt"] = filter_fmt
    if min_width is not None:
        params["min_width"] = min_width
    return bridge.call("list_frame_textures", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_texture_detail(resource_id: str) -> dict:
    """Texture metadata plus the full GetUsage event list."""
    return bridge.call("get_texture_detail", {"resource_id": resource_id}, timeout=_LONG_TIMEOUT)


@mcp.tool
def pixel_history(
    x: ReqInt,
    y: ReqInt,
    event_id: OptInt = None,
    resource_id: str | None = None,
    target: str = "color0",
    max_entries: LInt = 200,
) -> dict:
    """
    Modification history of one pixel: which draws wrote it, pre/post color,
    and why a fragment failed (depth, stencil, discard, scissor, backface).
    Needs a render target via event_id+target or resource_id.
    max_entries caps the returned list (total_count / truncated still report the full size).
    """
    params: dict[str, object] = {"x": x, "y": y, "target": target, "max_entries": max_entries}
    if event_id is not None:
        params["event_id"] = event_id
    if resource_id is not None:
        params["resource_id"] = resource_id
    return bridge.call("pixel_history", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def read_texture_stats(
    event_id: OptInt = None,
    resource_id: str | None = None,
    target: str = "color0",
) -> dict:
    """
    GPU min/max of a texture (GetMinMax). Depth targets are queried as CompType.Depth.
    Flags: has_negative / has_inf / has_nan / all_zero.
    """
    params: dict[str, object] = {"target": target}
    if event_id is not None:
        params["event_id"] = event_id
    if resource_id is not None:
        params["resource_id"] = resource_id
    return bridge.call("read_texture_stats", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def sample_pixel_region(
    event_id: OptInt = None,
    resource_id: str | None = None,
    target: str = "color0",
    x: LInt = 0,
    y: LInt = 0,
    width: OptInt = None,
    height: OptInt = None,
    sample_count: LInt = 256,
    anomaly_threshold: float = 10.0,
) -> dict:
    """
    Grid-sample a region and report NaN / Inf / negative / extreme-bright hotspots.
    """
    params: dict[str, object] = {
        "target": target, "x": x, "y": y,
        "sample_count": sample_count, "anomaly_threshold": anomaly_threshold,
    }
    if event_id is not None:
        params["event_id"] = event_id
    if resource_id is not None:
        params["resource_id"] = resource_id
    if width is not None:
        params["width"] = width
    if height is not None:
        params["height"] = height
    return bridge.call("sample_pixel_region", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_resource_usage(resource_id: str) -> dict:
    """Usage history of a texture or buffer: event_id, usage enum, read/write/clear kind."""
    return bridge.call("get_resource_usage", {"resource_id": resource_id}, timeout=_LONG_TIMEOUT)


@mcp.tool
def list_buffers(
    size_min: LInt = 0,
    size_max: LInt = 0,
    name_filter: str = "",
    group_by_size: bool = False,
    group_by_name: bool = False,
    with_usage: bool = False,
    unused_only: bool = False,
    limit: LInt = 0,
) -> dict:
    """
    Census of the capture's buffers (id, byte length, name).

    Use it to answer "why are there 1300 buffers": group_by_size shows how many
    buffers share a size (hundreds of identical small UBOs point at per-draw
    buffer churn), group_by_name groups by name with trailing digits stripped.
    unused_only lists buffers that no event ever binds, copies or clears (dead
    weight, "ghost" buffers); with_usage adds usage_count / first_event / last_event
    per buffer (one GetUsage per buffer, slower on big captures).

    Args:
        size_min / size_max: byte length bounds, 0 = unbounded
        name_filter: case-insensitive substring of the resource name
        group_by_size / group_by_name: return a histogram instead of the raw list
        limit: cap the raw list, 0 = all
    """
    params: dict[str, object] = {
        "size_min": size_min, "size_max": size_max, "name_filter": name_filter,
        "group_by_size": group_by_size, "group_by_name": group_by_name,
        "with_usage": with_usage, "unused_only": unused_only, "limit": limit,
    }
    return bridge.call("list_buffers", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_buffer_bindings_at_event(event_id: ReqInt) -> dict:
    """
    Every buffer bound at one draw call: vertex buffers (slot, offset, stride),
    the index buffer, and the constant buffers of each shader stage with the
    bound resource and byte range. Constant buffers are listed by reflection
    index, the same index read_constant_buffer takes.
    """
    return bridge.call("get_buffer_bindings_at_event", {"event_id": event_id}, timeout=_LONG_TIMEOUT)


def _classify_pipeline_diff(differences: list[dict]) -> dict:
    """Group compare_pipeline_states differences by what they mean for batching.

    Only restates facts about the two draws; it does not predict engine behaviour.
    """
    groups: dict[str, list[str]] = {
        "shader": [], "textures": [], "constant_buffer_resource": [], "constant_buffer_offset": [],
        "mesh": [], "render_state": [], "targets": [],
    }
    for d in differences:
        path = d["path"]
        if path.startswith("shaders.") and path.endswith(".shader"):
            groups["shader"].append(path)
        elif ".textures." in path:
            groups["textures"].append(path)
        elif ".constant_buffers." in path:
            # values look like "ResourceId::7022+0/384": same buffer, different offset is
            # the per-draw slice of one big buffer; a different buffer is a different upload
            a, b = str(d["a"]), str(d["b"])
            same_buffer = a.split("+")[0] == b.split("+")[0]
            groups["constant_buffer_offset" if same_buffer else "constant_buffer_resource"].append(path)
        elif path.startswith(("vertex_buffers", "index_buffer")):
            groups["mesh"].append(path)
        elif path.startswith(("render_targets", "depth_target")):
            groups["targets"].append(path)
        else:
            groups["render_state"].append(path)
    groups = {k: v for k, v in groups.items() if v}

    notes = []
    if not groups:
        notes.append("The two draws have identical pipeline state.")
    elif set(groups) <= {"mesh", "constant_buffer_offset"}:
        notes.append("Shader, textures, render state and targets are identical; only the mesh and the "
                     "offset into the same constant buffer differ. That is the per-draw slice a "
                     "SRP-Batcher-style submission produces. Merging into one instanced draw would "
                     "additionally need the same mesh.")
    else:
        blockers = [k for k in ("shader", "textures", "constant_buffer_resource", "render_state", "targets")
                    if k in groups]
        if blockers:
            notes.append("These differ and each needs its own state change between the draws: %s."
                         % ", ".join(blockers))
        if "mesh" in groups:
            notes.append("The meshes differ as well, so they cannot be one instanced draw.")
    return {"categories": groups, "notes": notes}


@mcp.tool
def compare_pipeline_states(event_id_a: ReqInt, event_id_b: ReqInt) -> dict:
    """
    Diff the pipeline state at two events and list only the fields that differ:
    bound shaders, textures (by unit), constant buffers (by reflection index,
    with buffer offset and size), vertex / index buffers, render targets,
    topology and, where the API exposes them, blend / depth / stencil / rasterizer.

    Answers "these two draws look the same, why were they not batched?": a
    different constant buffer offset or texture unit is the usual culprit.
    Verdict is IDENTICAL when nothing differs.

    The result also carries "batching": the differences grouped into shader /
    textures / constant_buffer_resource / constant_buffer_offset / mesh /
    render_state / targets, with a plain statement of what that means for merging
    the two draws (e.g. only mesh + same-buffer offset differing).
    """
    result = bridge.call(
        "compare_pipeline_states",
        {"event_id_a": event_id_a, "event_id_b": event_id_b},
        timeout=_LONG_TIMEOUT,
    )
    result["batching"] = _classify_pipeline_diff(result.get("differences", []))
    return result


@mcp.tool
def stat_instancing_efficiency(marker_filter: str = "", top_n: LInt = 10) -> dict:
    """
    Instancing statistics for the frame: how many draws are instanced, instance
    count spread, and "wasted" instanced draws (2 instances or fewer, no better
    than plain draws). Bucketed by the first two marker levels, i.e. per pass.
    Cheap: reads only the action tree. Use it to check whether SRP batcher /
    GPU instancing is actually merging draws, then drill into a pass with
    compare_pipeline_states.

    Args:
        marker_filter: only draws whose marker path contains this text
        top_n: how many highest-instance-count draws to list
    """
    return bridge.call(
        "stat_instancing_efficiency",
        {"marker_filter": marker_filter, "top_n": top_n},
        timeout=_LONG_TIMEOUT,
    )


@mcp.tool
def list_shaders(
    stage: StageVPCOpt = None,
    top_n: LInt = 50,
) -> dict:
    """
    Unique shaders in the frame (draw + dispatch) with usage counts.
    Omit stage to list vertex, pixel and compute.
    """
    params: dict[str, object] = {"top_n": top_n}
    if stage is not None:
        params["stage"] = stage
    return bridge.call("list_shaders", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def analyze_state_changes(
    marker_filter: str | None = None,
    change_types: list[str] | None = None,
    max_draws: LInt = 200,
) -> dict:
    """
    Count shader / blend / depth / cull / render_target changes between
    consecutive draws. Reports identical-state runs that could be instanced.
    """
    params: dict[str, object] = {"max_draws": max_draws}
    if marker_filter is not None:
        params["marker_filter"] = marker_filter
    if change_types is not None:
        params["change_types"] = change_types
    return bridge.call("analyze_state_changes", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_post_vs_data(
    event_id: ReqInt,
    stage: Literal["vsin", "vsout", "gsout"] = "vsout",
    max_vertices: LInt = 100,
) -> dict:
    """
    Post-transform vertex attributes (VS input, VS output, or GS output).
    Returns attribute layout plus up to max_vertices unpacked floats.
    """
    return bridge.call(
        "get_post_vs_data",
        {"event_id": event_id, "stage": stage, "max_vertices": max_vertices},
        timeout=_LONG_TIMEOUT,
    )


@mcp.tool
def export_mesh(event_id: ReqInt, output_path: str) -> dict:
    """Export the draw's post-VS mesh as an OBJ (position / normal / uv when present)."""
    return bridge.call(
        "export_mesh",
        {"event_id": event_id, "output_path": output_path},
        timeout=_LONG_TIMEOUT,
    )


@mcp.tool
def get_debug_messages(
    min_severity: Literal["info", "low", "medium", "high"] = "info",
    category: str | None = None,
    source: str | None = None,
    text_filter: str | None = None,
    event_id_min: OptInt = None,
    event_id_max: OptInt = None,
    max_messages: LInt = 100,
    max_groups: LInt = 30,
) -> dict:
    """
    API validation / driver debug messages recorded with the capture (Vulkan validation layers,
    GL/GLES KHR_debug output, the D3D debug layer) plus RenderDoc's own replay-time warnings.

    First thing to read when a frame renders wrongly: an invalid descriptor, a wrong image layout,
    a missing barrier or a GL error is often reported here with the event it happened at.
    Identical messages are grouped (message_id + text) with a count and sample events; by_severity /
    by_category / by_source / total_matched cover every matching message, groups and messages are capped.

    Args:
        min_severity: keep this severity or worse (info < low < medium < high)
        category: name or substring, e.g. "Performance", "Execution", "Shaders", "State_Setting"
        source: name or substring, e.g. "IncorrectAPIUse", "RedundantAPIUse", "GeneralPerformance"
        text_filter: case-insensitive substring of the message text
        event_id_min / event_id_max: only messages raised inside this event range
    Note: validation messages exist only if the capture was recorded with the validation layer / debug
    context on; an empty result does not prove the frame is valid.
    """
    params: dict[str, object] = {
        "min_severity": min_severity, "max_messages": max_messages, "max_groups": max_groups,
    }
    for key, value in (("category", category), ("source", source), ("text_filter", text_filter),
                       ("event_id_min", event_id_min), ("event_id_max", event_id_max)):
        if value is not None:
            params[key] = value
    return bridge.call("get_debug_messages", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def render_overlay(
    event_id: ReqInt,
    overlay: Literal[
        "depth", "stencil", "backface_cull", "drawcall", "wireframe", "viewport_scissor", "nan", "clipping",
        "quad_overdraw_draw", "quad_overdraw_pass", "triangle_size_draw", "triangle_size_pass",
    ] = "depth",
    target: str = "color0",
    resource_id: str | None = None,
    output_path: str | None = None,
    analyze: bool = True,
) -> dict:
    """
    Render one of RenderDoc's debug overlays at a draw call on the replay device (the phone, for remote
    replay), save it as a PNG you can look at, and summarise it in numbers.

    The first tool to reach for when "why is this object not visible / wrong / expensive":
    - depth / stencil: pixels of THIS draw that passed (green) or failed (red) the test. Everything failed =
      hidden behind earlier geometry or wrong depth func/clear; passed but nothing visible = look elsewhere.
    - backface_cull: kept (green) vs culled (red). Everything culled = wrong winding / cull mode.
    - drawcall / wireframe: where the draw is and its triangle edges (wire pixel count).
    - viewport_scissor: also returns the viewport and scissor rectangles; a draw clipped by a wrong scissor
      shows up here.
    - nan / clipping: pixels marked (count, colours); 0 marked = clean.
    - quad_overdraw_draw / _pass: overdraw per pixel (this draw / the whole pass up to it): mean, max,
      share of pixels shaded more than once, histogram. Counts only fragments that passed the depth test, in
      2x2 quads (so edges between triangles show 2). The pass variant is the number to compare before/after
      sorting or culling changes on a mobile GPU.
    - triangle_size_draw / _pass: pixels by the area of the triangle covering them (<4 px, 4-16, ...);
      lots of pixels in tiny triangles = geometry too dense for its screen size. Pixel-weighted: micro
      triangles cover few pixels, so cross-check with estimate_geometry_cost.

    Args:
        event_id: a draw call (overlays need a draw; markers have none)
        target: "color0".. or "depth" of that event (Vulkan: taken from the render pass), or give resource_id
        output_path: where to write the PNG (default %TEMP%/renderdoc_mcp/overlays/overlay_e<event>_<overlay>.png);
            open it to see the overlay
        analyze: also read the overlay back and count (about 1 s on a phone, more transfer on big targets);
            False only saves the image
    clear_before_pass / clear_before_draw are not offered: their overlay texture carries no data.
    """
    params: dict[str, object] = {"event_id": event_id, "overlay": overlay, "target": target, "analyze": analyze}
    if resource_id is not None:
        params["resource_id"] = resource_id
    if output_path is not None:
        params["output_path"] = output_path
    return bridge.call("render_overlay", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_api_calls(
    event_id: OptInt = None,
    start_event: OptInt = None,
    end_event: OptInt = None,
    include_params: bool = True,
    name_filter: str = "",
    max_calls: LInt = 200,
) -> dict:
    """
    Raw API calls recorded in the capture, with their parameter values.

    list/get_draw_calls only show draws, clears and markers. The state setup around
    them (glUniform*, glBindBuffer, glBlendFunc, glEnable, barriers, ...) is not
    visible there; this tool exposes it. Pick calls either by

    - event_id: the calls that belong to that action, i.e. the setup before the draw
    - start_event + end_event: every call whose event id lies in the range

    Args:
        include_params: include each call's argument values (resource ids, enums
            as names, structs/arrays nested; arrays are cut at 64 elements)
        name_filter: case-insensitive substring of the API function name,
            e.g. "Uniform", "Bind", "Blend"
        max_calls: cap on returned calls (max 1000); matched / truncated tell you
            if more existed
    Each call carries event_id, name, duration_us when recorded, and params.
    """
    params: dict[str, object] = {
        "include_params": include_params, "name_filter": name_filter, "max_calls": max_calls,
    }
    if event_id is not None:
        params["event_id"] = event_id
    if start_event is not None:
        params["start_event"] = start_event
    if end_event is not None:
        params["end_event"] = end_event
    return bridge.call("get_api_calls", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def find_pixels(
    filters: dict[str, float | bool],
    event_id: OptInt = None,
    resource_id: str | None = None,
    target: str = "color0",
    x: LInt = 0,
    y: LInt = 0,
    width: OptInt = None,
    height: OptInt = None,
    max_probes: LInt = 1024,
    max_results: LInt = 50,
) -> dict:
    """
    Find pixels in a render target or texture that satisfy channel conditions, and
    report where they are. Use it to locate black holes, blown-out highlights,
    one flat color, or NaN/Inf/negative pixels, then pixel_history on a hit.

    Args:
        filters: conditions that must ALL hold. Float ranges (inclusive)
            min_r / max_r / min_g / max_g / min_b / max_b / min_a / max_a, and
            booleans has_nan / has_inf / has_negative.
            e.g. {"max_r": 0.01, "max_g": 0.01, "max_b": 0.01} finds near-black pixels.
        event_id + target ("color0".. / "depth"), or resource_id: what to search
        x, y, width, height: sub-region (default the whole texture)
        max_probes: how many pixels to read (max 4096). One read per probe, so this
            is a SAMPLE: a region with <= max_probes pixels is covered fully,
            a larger one with an evenly spaced grid (probe_stride says how sparse).
            "exhaustive" in the result tells which case you got.
        max_results: matches to list (bounding_box and matched_probes cover all)
    """
    params: dict[str, object] = {
        "filters": filters, "target": target, "x": x, "y": y,
        "max_probes": max_probes, "max_results": max_results,
    }
    if event_id is not None:
        params["event_id"] = event_id
    if resource_id is not None:
        params["resource_id"] = resource_id
    if width is not None:
        params["width"] = width
    if height is not None:
        params["height"] = height
    return bridge.call("find_pixels", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def get_cached_message(
    message_id: str,
    page: LInt = 1,
    page_size: LInt = 50,
    collection: str | None = None,
    search: str | None = None,
) -> dict:
    """
    Read a tool result that was too large to return directly.

    Any tool whose result exceeds the size limit (default 20000 chars, env
    RENDERDOC_MCP_MAX_RESPONSE_CHARS) returns {"status": "cached", "message_id": ...}
    with the size of each list in the result and a short preview. Use this tool
    to page through it or search it instead of re-running the original tool.

    Args:
        message_id: The id from the "cached" response
        page / page_size: 1-based page and items per page (max 200)
        collection: Which view to page (see available_views): a list, paged by item (default: the
            largest list), or a large non-list field such as a shader source, paged by line
        search: Case-insensitive substring; only items containing it are returned
    The cache lives in the MCP server process (last 64 results); an evicted or
    restarted-away id has to be re-fetched by re-running the original tool.
    """
    if collection is not None and not collection.strip():
        collection = None
    if search is not None and not search.strip():
        search = None
    return result_cache.query_cached_message(message_id, page, page_size, collection, search)


# ==================== Before / after comparison ====================


def _collect_snapshot(name: str, include_shading_cost: bool, compiler: str, gpu: str | None,
                      include_draw_analysis: bool = False) -> dict:
    """Run the analysis tools on the loaded capture and condense them into a snapshot"""
    status = bridge.call("get_capture_status")
    summary = bridge.call("get_frame_summary")
    draws = summary.get("statistics", {}).get("draw_calls") or 200
    pass_list = bridge.call("get_renderpass_list", {"sort_by": "index"}, timeout=_LONG_TIMEOUT)
    instancing = bridge.call("stat_instancing_efficiency", {"marker_filter": "", "top_n": 1}, timeout=_LONG_TIMEOUT)
    state = bridge.call("analyze_state_changes", {"max_draws": int(draws)}, timeout=_LONG_TIMEOUT)
    textures = bridge.call("list_frame_textures", {"sort_by": "size", "top_n": 1}, timeout=_LONG_TIMEOUT)
    risks = bridge.call("diagnose_mobile_risks", {"severity_filter": "all"}, timeout=_LONG_TIMEOUT)
    bandwidth = bridge.call("analyze_pass_bandwidth", {"top_n": 1000, "sort_by": "index"}, timeout=_LONG_TIMEOUT)
    shading = geometry = over = tex_bw = None
    warnings = []
    _, default_gpu, _, _ = _compiler(compiler)
    gpu = gpu or default_gpu
    if include_shading_cost or include_draw_analysis:
        try:
            # one coverage pass and one pixel-source pass feed shading and overdraw; vertex sources feed geometry
            # ONE replay pass yields coverage, state, textures and the pixel (and vertex) sources:
            # a SetFrameEvent per draw is ~95% of every per-draw analysis, so separate calls triple the cost
            stages = ["pixel", "vertex"] if include_draw_analysis else ["pixel"]
            coverage = bridge.call(
                "get_draw_coverage",
                {"with_state": True, "with_textures": include_draw_analysis,
                 "with_sources": {"stages": stages, "fold_uniforms": True}},
                timeout=_LONG_TIMEOUT)
            pixel_data = coverage["sources"]["pixel"]
            pixel_reports = _compile_sources(pixel_data, "pixel", compiler, gpu)
            shading = _shading_result(coverage, pixel_data, pixel_reports, compiler, gpu, 200)
            if include_draw_analysis:
                over = _overdraw_result(coverage, pixel_data, pixel_reports, compiler, gpu, 10)
                vertex_data = coverage["sources"]["vertex"]
                vertex_reports = _compile_sources(vertex_data, "vertex", compiler, gpu)
                geometry = _geometry_result(coverage, vertex_data, vertex_reports, compiler, gpu, 10)
                tex_list = bridge.call("list_frame_textures", {"sort_by": "size", "top_n": 100000}, timeout=_LONG_TIMEOUT)
                tex_bw = texture_analysis.texture_footprint(tex_list["textures"], coverage["draws"], 10)
            if not include_shading_cost:
                shading = None
        except Exception as e:  # missing malioc/aoc must not lose the rest of the snapshot
            warnings.append("shading / draw analysis skipped: %s" % e)
    snap = compare.build_snapshot(
        name, status, summary, pass_list, instancing, state, textures, risks, bandwidth, shading, geometry, over, tex_bw,
    )
    if warnings:
        snap["warnings"] = warnings
    return snap


def _snapshot_brief(snap: dict, path: str) -> dict:
    out = {
        "name": snap["name"], "path": path, "capture": snap["capture"], "frame": snap["frame"],
        "bandwidth": dict((k, v) for k, v in snap["bandwidth"].items() if k not in ("passes", "issues")),
    }
    if snap.get("shading"):
        out["shading"] = dict((k, v) for k, v in snap["shading"].items() if k != "shaders")
    if snap.get("warnings"):
        out["warnings"] = snap["warnings"]
    return out


@mcp.tool
def snapshot_metrics(
    name: str,
    include_shading_cost: bool = True,
    compiler: Literal["mali", "adreno"] = "mali",
    gpu: str | None = None,
    save_dir: str | None = None,
    include_draw_analysis: bool = False,
) -> dict:
    """
    Condense the currently loaded capture into a metrics snapshot and save it as JSON,
    so it can be diffed against another capture with diff_snapshots.

    Collects: draw / pass counts, indices submitted, instancing, state changes, texture
    memory, mobile risks, tile-pass attachment bandwidth (analyze_pass_bandwidth) and,
    optionally, fragment shading cost per shader (estimate_shading_cost; needs malioc /
    aoc and, for mobile captures, remote replay). Runs several replay passes: expect
    minutes on a big capture.

    Args:
        name: snapshot name; also the file name (<save_dir>/<name>.json)
        include_shading_cost: skip it for a quick snapshot or when no offline compiler is installed
        compiler / gpu: forwarded to estimate_shading_cost
        save_dir: default is <temp>/renderdoc_mcp/snapshots
        include_draw_analysis: also run estimate_geometry_cost and analyze_overdraw (shares the same
            replay passes, adds one vertex-shader compile pass); needed for the geometry / overdraw
            sections of frame_performance_report
    """
    snap = _collect_snapshot(name, include_shading_cost, compiler, gpu, include_draw_analysis)
    path = compare.save_snapshot(snap, save_dir)
    return _snapshot_brief(snap, path)


@mcp.tool
def frame_performance_report(
    snapshot: str | None = None,
    include_shading_cost: bool = True,
    compiler: Literal["mali", "adreno"] = "mali",
    gpu: str | None = None,
    top_actions: LInt = 15,
    save_dir: str | None = None,
    include_draw_analysis: bool = True,
) -> dict:
    """
    One-call performance report for the loaded capture: a scorecard per area and a ranked
    list of concrete actions with evidence (events / markers) and impact.

    Areas: attachment bandwidth (wasted stores, loads without clear, reopened targets,
    readbacks), fragment shading (heaviest shaders by share of cycles, late-Z, pixels shaded
    then rejected by depth), submission (draw calls, identical-state runs that could be
    batched) and mobile risks; with include_draw_analysis also the vertex budget (vertex-heavy
    passes, micro-triangles) and overdraw (hidden opaque pixels, late-Z waste, blended layers,
    draw order) and texture sampling footprint (uncompressed / over-sized textures). Built from the same metrics as snapshot_metrics, which it
    saves, so the run takes minutes on a big capture; pass `snapshot` (a saved name or
    path) to build the report from an existing snapshot without touching RenderDoc.

    Impact percentages are relative to their own area, so they rank actions inside an area but
    are not comparable across areas. Cycles and bytes are relative costs, not milliseconds.

    Args:
        snapshot: name / path of a saved snapshot to report on instead of analysing the loaded capture
        include_shading_cost / compiler / gpu: as snapshot_metrics
        include_draw_analysis: add vertex budget (estimate_geometry_cost) and overdraw (analyze_overdraw)
            findings; a snapshot taken without it simply has no such sections
        top_actions: how many actions to return (the rest are counted in actions_total). Bandwidth
            issues keep the 3 heaviest of each type; the rest of a type become one "N more ..." action
            (actions_folded counts the entries merged away)
    """
    if snapshot:
        snap = compare.load_snapshot(snapshot, save_dir)
    else:
        status = bridge.call("get_capture_status")
        name = os.path.splitext(os.path.basename(status.get("filename") or "capture"))[0]
        snap = _collect_snapshot(name, include_shading_cost, compiler, gpu, include_draw_analysis)
        compare.save_snapshot(snap, save_dir)
    return report.build_report(snap, int(top_actions))


@mcp.tool
def diff_snapshots(
    before: str,
    after: str,
    save_dir: str | None = None,
) -> dict:
    """
    Compare two saved snapshots (names from snapshot_metrics, or paths to the JSON files).

    Returns a headline of the biggest improvements and regressions, a metrics table with
    before / after / delta / verdict (better / worse / same), tile passes whose attachment
    traffic changed, bandwidth issues resolved / new / persisting, per-shader fragment cost
    changes, and warnings when the two captures are not like-for-like (different GPU,
    resolution or API). Shader costs are cycles per pixel, so they stay comparable across
    different views; pixel and cycle totals depend on the camera.
    """
    return compare.diff_snapshots(
        compare.load_snapshot(before, save_dir), compare.load_snapshot(after, save_dir),
    )


@mcp.tool
def compare_captures(
    before_path: str,
    after_path: str,
    use_remote: bool | None = None,
    include_shading_cost: bool = True,
    compiler: Literal["mali", "adreno"] = "mali",
    gpu: str | None = None,
    save_dir: str | None = None,
    include_draw_analysis: bool = False,
) -> dict:
    """
    Open two captures one after the other, snapshot each and return their diff
    (open_capture + snapshot_metrics + diff_snapshots in one call).

    Meant for "did this optimisation work": before_path is the capture before the change,
    after_path the one after. Capture both on the same device, scene and resolution for a
    like-for-like result; diff warnings list any mismatch. Takes minutes (two loads plus
    two sets of replay analyses). The snapshots are saved, so diff_snapshots can be re-run
    later without reopening the captures. The "after" capture is left loaded.
    """
    def stem(path: str) -> str:
        return os.path.splitext(os.path.basename(path))[0]

    name_a, name_b = stem(before_path), stem(after_path)
    if name_a == name_b:
        name_a, name_b = name_a + "_before", name_b + "_after"

    snaps, paths = [], []
    for name, path in ((name_a, before_path), (name_b, after_path)):
        _open_capture(path, use_remote, _COMPARE_SETTLE)
        snaps.append(_collect_snapshot(name, include_shading_cost, compiler, gpu, include_draw_analysis))
        # saved right away: if the second capture fails, the first one's minutes of replay are kept
        paths.append(compare.save_snapshot(snaps[-1], save_dir))
    result = compare.diff_snapshots(snaps[0], snaps[1])
    result["snapshot_paths"] = paths
    return result


# ==================== Analysis prompts ====================


@mcp.prompt
def full_frame_analysis() -> str:
    """Complete frame analysis workflow: remote replay, pass overview, shading cost, then drill-down."""
    return """执行完整帧分析，按顺序调用工具，每步先核对结果再继续：

1. `get_replay_context` — 确认 Replay Context。移动端抓帧必须 remote_connected=true。
2. 若尚未打开抓帧：`open_capture(capture_path=..., use_remote=True)`（移动端）或省略 use_remote。
3. `get_capture_status` / `get_frame_summary` — API、DC 数、顶层 marker、GPU quirks。
4. `get_renderpass_list` — 看 Pass 划分和每个 Pass 的 DC / RT。
5. `list_frame_textures(sort_by="size", top_n=20)` — 最大/最常用贴图。
6. `get_shader_usage` — 按像素 shader 聚合，识别材质。
7. 移动端：`save_texture` 导出最后一事件的 color0，对照真机截图后再信后续数字。
8. 移动端：`estimate_shading_cost(gpu="Mali-G610")` 排着色热点；桌面：`get_action_timings`。
9. `diagnose_mobile_risks` — 精度 / 性能 / 兼容性 / GPU 特性。
10. 综合给出按影响排序的建议：Pass 是否过多、贴图是否过大、哪些 shader 最贵、有无被遮挡的大 DC。"""


@mcp.prompt
def debug_draw_call(event_id: int) -> str:
    """Deep-dive a single draw call: pipeline, bindings, shader, output."""
    return f"""逐步分析 event {event_id} 的 DrawCall：

1. `get_draw_call_details(event_id={event_id})` — 顶点/实例/输出。
2. `get_pipeline_state(event_id={event_id})` — 绑定的 shader、RT、viewport、blend。
3. `get_shader_info(event_id={event_id}, stage="pixel")` 和 stage="vertex"。
4. `get_shader_source(event_id={event_id}, fold_uniforms=True)` — GLES 时还原材质分支。
5. `analyze_shader_cost(event_ids=[{event_id}], gpu="Mali-G610")` — 移动端静态开销。
6. `save_texture` / `pick_pixel` 检查该事件后的 color0 / depth。
7. 总结这个 DC 画了什么、绑定是否合理、有无精度或 discard / late-Z 风险。"""


@mcp.prompt
def analyze_performance() -> str:
    """Performance workflow: rank passes and shaders, then mobile risks."""
    return """分析这一帧的性能问题：

0. 先试 `frame_performance_report` — 一次给出记分卡和按影响排序的优化项（带宽 / 着色 / 提交 / 风险），
   已有快照时传 snapshot=名字 即可秒出。下面的步骤用于深挖报告里点名的 event / Pass。
1. `get_replay_context` + `get_frame_summary` — 确认回放设备和帧规模。
2. `get_renderpass_list(sort_by="draw_count")` — DC 最多的 Pass。
1b. 做完优化后：`compare_captures(before_path, after_path)` 验证效果，先看 headline 和 shading.total_shader_cost_delta。
2a. `estimate_geometry_cost` — 顶点/三角形预算和微小三角形；`analyze_overdraw` — 不透明被遮挡、late-Z 浪费、透明层数、绘制顺序。
2b. 移动端：`analyze_pass_bandwidth` — 附件 Load/Store 带宽，先处理 high 级问题（多余的 Store、被打断重开的 RT、没 clear 的 Load）。
3. 移动端（Mali/Adreno/PowerVR）：
   - 不要用 `get_action_timings` 排热点（tile flush 会把时间撑大）。
   - `estimate_shading_cost` 按「像素 × malioc 周期」排序。
   - `get_draw_coverage` 看遮挡比高的 DC。
4. 桌面 GPU：`get_action_timings` 看耗时最长的 action。
5. `list_frame_textures(sort_by="size")` — 最大贴图和未使用 mip。
6. `analyze_state_changes` — shader / RT 切换是否过碎，有无可合批区间。
7. `diagnose_mobile_risks` — 全屏 Clear 过多、超大贴图、R11G11B10、discard。
8. 按影响列出建议：是什么、在哪个 event/Pass、严重程度、怎么改。"""


@mcp.prompt
def find_rendering_issue(description: str) -> str:
    """Systematic visual-bug diagnosis from a problem description."""
    return f"""根据问题描述系统排查渲染异常。

问题：{description}

1. `get_frame_summary` + `get_renderpass_list` — 锁定相关 Pass（按 RT / marker）。
2. `get_pass_detail` 看该 Pass 里的 DC。
3. 对可疑 DC：
   - `get_pipeline_state` / `get_draw_call_details`
   - `save_texture` 或 `pick_pixel` 看输出
   - `read_texture_stats` 查 NaN / Inf / 负值 / 全 0
4. 像素异常（黑块、花屏、闪点）：
   - `sample_pixel_region` 扫区域
   - `pixel_history` 追哪些 DC 写过这个像素、depth/stencil/discard 是否失败
5. 精度怀疑：`diagnose_mobile_risks(check_categories=["precision"])`
6. 给出根因和修复建议，并附 event_id / resource_id 证据。"""


def main():
    """Run the MCP server"""
    mcp.run()


if __name__ == "__main__":
    main()
