"""
RenderDoc MCP Server
FastMCP 2.0 server providing access to RenderDoc capture data.
"""

from typing import Literal

from fastmcp import FastMCP

from .bridge.client import RenderDocBridge, RenderDocBridgeError
from .config import settings

# Initialize FastMCP server
mcp = FastMCP(
    name="RenderDoc MCP Server",
)

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
    event_id_min: int | None = None,
    event_id_max: int | None = None,
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
    return bridge.call("get_draw_calls", params)


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
    stage: Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"] | None = None,
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
    return bridge.call("find_draws_by_shader", params)


@mcp.tool
def find_draws_by_texture(texture_name: str) -> dict:
    """
    Find all draw calls using a texture with the given name (partial match).

    Args:
        texture_name: Partial name to search for in texture resource names

    Returns a list of matching draw calls with event IDs and match reasons.
    Searches SRVs, UAVs, and render targets.
    """
    return bridge.call("find_draws_by_texture", {"texture_name": texture_name})


@mcp.tool
def find_draws_by_resource(resource_id: str) -> dict:
    """
    Find all draw calls using a specific resource ID (exact match).

    Args:
        resource_id: Resource ID to search for (e.g. "ResourceId::12345" or "12345")

    Returns a list of matching draw calls with event IDs and match reasons.
    Searches shaders, SRVs, UAVs, render targets, and depth targets.
    """
    return bridge.call("find_draws_by_resource", {"resource_id": resource_id})


@mcp.tool
def get_draw_call_details(event_id: int) -> dict:
    """
    Get detailed information about a specific draw call.

    Args:
        event_id: The event ID of the draw call to inspect

    Includes vertex/index counts, resource outputs, and other metadata.
    """
    return bridge.call("get_draw_call_details", {"event_id": event_id})


@mcp.tool
def get_action_timings(
    event_ids: list[int] | None = None,
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
    return bridge.call("get_action_timings", params)


@mcp.tool
def get_shader_info(
    event_id: int,
    stage: Literal["vertex", "hull", "domain", "geometry", "pixel", "compute"],
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
    offset: int = 0,
    length: int = 0,
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
    mip: int = 0,
    slice: int = 0,
    sample: int = 0,
    depth_slice: int | None = None,
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
    return bridge.call("get_texture_data", params)


@mcp.tool
def get_pipeline_state(event_id: int) -> dict:
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
    params: dict[str, object] = {"capture_path": capture_path}
    if use_remote is not None:
        params["use_remote"] = use_remote
    return bridge.call("open_capture", params)


# ==================== Performance analysis ====================

# Long-running analysis calls replay many events; give them more time than the default 30s
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
    event_ids: list[int] | None = None,
    marker_filter: str | None = None,
    event_id_min: int | None = None,
    event_id_max: int | None = None,
    summary_only: bool = False,
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
    """
    data = bridge.call(
        "get_draw_coverage",
        _selection(event_ids, marker_filter, event_id_min, event_id_max),
        timeout=_LONG_TIMEOUT,
    )
    if summary_only:
        data = {k: v for k, v in data.items() if k != "draws"}
    return data


@mcp.tool
def get_shader_usage(
    marker_filter: str | None = None,
    event_id_min: int | None = None,
    event_id_max: int | None = None,
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
    event_id: int,
    stage: Literal["vertex", "pixel"] = "pixel",
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


def _shader_cost(selection: dict[str, object], stage: str, gpu: str, fold_uniforms: bool,
                 overrides: dict[str, str] | None) -> tuple[dict, dict]:
    from . import malioc

    params = dict(selection, stage=stage, fold_uniforms=fold_uniforms)
    if overrides:
        params["overrides"] = overrides
    data = bridge.call("get_shader_sources", params, timeout=_LONG_TIMEOUT)
    reports = malioc.analyze_many(data["sources"], stage=stage, gpu=gpu)
    return data, reports


@mcp.tool
def analyze_shader_cost(
    event_ids: list[int] | None = None,
    marker_filter: str | None = None,
    event_id_min: int | None = None,
    event_id_max: int | None = None,
    stage: Literal["vertex", "pixel"] = "pixel",
    gpu: str = "Mali-G610",
    fold_uniforms: bool = True,
    overrides: dict[str, str] | None = None,
) -> dict:
    """
    Static shader cost with the Mali Offline Compiler (malioc) for the selected draws.

    With fold_uniforms (default), each draw's UnityPerMaterial values are folded
    into the source, so the reported cycles are for the path that material
    actually executes (e.g. disabled effect branches are dropped). Identical
    folded sources are compiled once.
    Caveat: folding also lets the compiler constant-fold plain math such as
    lerp(x, y, 0) that the real GPU still executes. It is accurate for code
    behind uniform branches, but underestimates branch-free shaders; use
    fold_uniforms=False for the cost the GPU actually pays there.

    Returns per unique source: bound cycles per pixel/vertex, per-pipeline cycles
    (arith/varying/texture...), work registers, thread occupancy, fp16 ratio and
    late-ZS / coverage flags (discard), plus which draws use it.
    Use the device GPU name for 'gpu' (e.g. "Mali-G610", "Mali-G710", "Mali-G76").
    Requires malioc (Arm Performance Studio) on PATH or MALIOC_PATH.
    """
    from . import malioc

    data, reports = _shader_cost(
        _selection(event_ids, marker_filter, event_id_min, event_id_max),
        stage, gpu, fold_uniforms, overrides,
    )
    by_source: dict[str, dict] = {}
    for draw in data["draws"]:
        sid = draw.get("source_id")
        if sid is None:
            continue
        entry = by_source.setdefault(sid, {
            "source_id": sid, "shader": draw["shader"], "draw_count": 0,
            "sample_event_ids": [], "textures": draw["textures"], "malioc": reports.get(sid),
        })
        entry["draw_count"] += 1
        if len(entry["sample_event_ids"]) < 5:
            entry["sample_event_ids"].append(draw["event_id"])
    sources = sorted(by_source.values(), key=lambda e: -e["draw_count"])
    return {
        "gpu": gpu,
        "malioc_version": malioc.malioc_version(),
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
    event_id_min: int | None = None,
    event_id_max: int | None = None,
    event_ids: list[int] | None = None,
    gpu: str = "Mali-G610",
    fold_uniforms: bool = True,
    top_n: int = 30,
) -> dict:
    """
    Estimate where fragment shading cost goes in a frame:
    pixels shaded per draw (depth-test overlay, measured on the replay device)
    × malioc bound cycles per pixel of that draw's material-folded shader.

    Results are grouped by pixel shader with the dominant textures to identify
    the material. Use it to rank shaders and to compare before/after captures
    (cycles/pixel is view-independent; total cycles depends on the view).
    Run it with remote replay for mobile captures.
    """
    from . import malioc

    selection = _selection(event_ids, marker_filter, event_id_min, event_id_max)
    coverage = bridge.call("get_draw_coverage", selection, timeout=_LONG_TIMEOUT)
    data, reports = _shader_cost(selection, "pixel", gpu, fold_uniforms, None)
    source_of = {d["event_id"]: d.get("source_id") for d in data["draws"]}

    groups: dict[str, dict] = {}
    total_cycles = 0.0
    for draw in coverage["draws"]:
        sid = source_of.get(draw["event_id"])
        report = reports.get(sid) if sid else None
        cyc = report.get("bound_cycles") if report and "error" not in report else None
        g = groups.setdefault(draw["pixel_shader"], {
            "pixel_shader": draw["pixel_shader"], "draw_count": 0, "pixels": 0, "occluded_pixels": 0,
            "cycles": 0.0, "textures": {}, "sample_event_ids": [], "late_zs_draws": 0,
            "work_registers": set(), "unknown_cost_draws": 0,
        })
        g["draw_count"] += 1
        g["pixels"] += draw["passed"]
        g["occluded_pixels"] += draw["failed"]
        if cyc is None:
            g["unknown_cost_draws"] += 1
        else:
            g["cycles"] += draw["passed"] * cyc
            total_cycles += draw["passed"] * cyc
            if report.get("uses_late_zs_update") or report.get("modifies_coverage"):
                g["late_zs_draws"] += 1
            if report.get("work_registers") is not None:
                g["work_registers"].add(report["work_registers"])
        for t in draw["textures"]:
            g["textures"][t] = g["textures"].get(t, 0) + 1
        if len(g["sample_event_ids"]) < 5:
            g["sample_event_ids"].append(draw["event_id"])

    rows = []
    for g in groups.values():
        g["cycles_per_pixel"] = round(g["cycles"] / g["pixels"], 3) if g["pixels"] else None
        g["share_of_cycles"] = round(g["cycles"] / total_cycles, 4) if total_cycles else None
        g["cycles"] = round(g["cycles"])
        g["textures"] = [n for n, _ in sorted(g["textures"].items(), key=lambda kv: -kv[1])[:4]]
        g["work_registers"] = sorted(g["work_registers"])
        rows.append(g)
    rows.sort(key=lambda g: -g["cycles"])
    return {
        "gpu": gpu,
        "malioc_version": malioc.malioc_version(),
        "draw_count": coverage["draw_count"],
        "total_pixels": coverage["total_passed"],
        "total_occluded_pixels": coverage["total_failed"],
        "total_cycles": round(total_cycles),
        "note": "cycles = sum(pixels_passed x malioc bound cycles); relative ranking, not wall time",
        "shaders": rows[:top_n],
    }


@mcp.tool
def save_texture(
    output_path: str,
    event_id: int | None = None,
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
    x: int,
    y: int,
    event_id: int | None = None,
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
def get_pass_detail(pass_index: int) -> dict:
    """
    List draw calls and attachments of one pass from get_renderpass_list.
    """
    return bridge.call("get_pass_detail", {"pass_index": pass_index}, timeout=_LONG_TIMEOUT)


@mcp.tool
def list_frame_textures(
    sort_by: Literal["size", "usage", "name"] = "size",
    top_n: int = 50,
    filter_fmt: str | None = None,
    min_width: int | None = None,
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
    x: int,
    y: int,
    event_id: int | None = None,
    resource_id: str | None = None,
    target: str = "color0",
) -> dict:
    """
    Modification history of one pixel: which draws wrote it, pre/post color,
    and why a fragment failed (depth, stencil, discard, scissor, backface).
    Needs a render target via event_id+target or resource_id.
    """
    params: dict[str, object] = {"x": x, "y": y, "target": target}
    if event_id is not None:
        params["event_id"] = event_id
    if resource_id is not None:
        params["resource_id"] = resource_id
    return bridge.call("pixel_history", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def read_texture_stats(
    event_id: int | None = None,
    resource_id: str | None = None,
    target: str = "color0",
) -> dict:
    """
    GPU min/max of a texture (GetMinMax). Flags has_negative / has_inf / has_nan / all_zero.
    """
    params: dict[str, object] = {"target": target}
    if event_id is not None:
        params["event_id"] = event_id
    if resource_id is not None:
        params["resource_id"] = resource_id
    return bridge.call("read_texture_stats", params, timeout=_LONG_TIMEOUT)


@mcp.tool
def sample_pixel_region(
    event_id: int | None = None,
    resource_id: str | None = None,
    target: str = "color0",
    x: int = 0,
    y: int = 0,
    width: int | None = None,
    height: int | None = None,
    sample_count: int = 256,
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
def list_shaders(
    stage: Literal["vertex", "pixel", "compute"] | None = None,
    top_n: int = 50,
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
    max_draws: int = 200,
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

1. `get_replay_context` + `get_frame_summary` — 确认回放设备和帧规模。
2. `get_renderpass_list(sort_by="draw_count")` — DC 最多的 Pass。
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
