"""
Mali Offline Compiler (malioc) wrapper.

Runs in the MCP server process (full Python), not inside RenderDoc, because
qrenderdoc's embedded Python cannot reliably spawn subprocesses.
"""

import base64
import glob
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor


_CANDIDATES = [
    r"C:\Program Files\Arm\Arm Performance Studio*\mali_offline_compiler\malioc.exe",
    r"D:\Program Files\Arm\Arm Performance Studio*\mali_offline_compiler\malioc.exe",
    r"C:\Program Files\Arm\Arm Mobile Studio*\mali_offline_compiler\malioc.exe",
]


class MaliocError(Exception):
    """malioc not found or failed"""

    pass


def find_malioc() -> str:
    """MALIOC_PATH env > malioc on PATH > Arm Performance Studio install dirs"""
    env = os.environ.get("MALIOC_PATH")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("malioc")
    if found:
        return found
    for pattern in _CANDIDATES:
        hits = sorted(glob.glob(pattern), reverse=True)
        if hits:
            return hits[0]
    raise MaliocError(
        "malioc not found. Install Arm Performance Studio or set MALIOC_PATH to malioc.exe"
    )


def malioc_version() -> str:
    out = subprocess.run([find_malioc(), "--version"], capture_output=True, text=True, timeout=30)
    lines = out.stdout.strip().splitlines()
    return lines[0] if lines else "unknown"


def _summarize(data: dict) -> dict:
    """Condense malioc JSON output (schema used by malioc 7.x and 2020+ releases)"""
    shader = data["shaders"][0]
    props = {p["name"]: p["value"] for p in shader.get("properties", [])}
    variant = next((v for v in shader.get("variants", []) if v.get("name") == "Main"), shader["variants"][0])
    vprops = {p["name"]: p["value"] for p in variant.get("properties", [])}
    perf = variant["performance"]
    pipelines = perf["pipelines"]

    def cycles(key):
        block = perf.get(key) or {}
        values = block.get("cycle_count") or []
        return {
            "cycles": {p: round(v, 3) for p, v in zip(pipelines, values) if v is not None},
            "bound": block.get("bound_pipelines", []),
        }

    longest = cycles("longest_path_cycles")
    bound_cycles = max(longest["cycles"].values()) if longest["cycles"] else None
    return {
        "hardware": shader.get("hardware", {}).get("core") if isinstance(shader.get("hardware"), dict) else None,
        "longest_path": longest,
        "shortest_path": cycles("shortest_path_cycles"),
        "bound_cycles": bound_cycles,
        "work_registers": vprops.get("work_registers_used"),
        "thread_occupancy": vprops.get("thread_occupancy"),
        "fp16_arithmetic": vprops.get("fp16_arithmetic"),
        "stack_spilling": vprops.get("has_stack_spilling"),
        "modifies_coverage": props.get("modifies_coverage"),
        "uses_late_zs_test": props.get("uses_late_zs_test"),
        "uses_late_zs_update": props.get("uses_late_zs_update"),
        "reads_color_buffer": props.get("reads_color_buffer"),
        "warnings": shader.get("warnings", []),
    }


def analyze_source(source: str, stage: str = "pixel", gpu: str = "Mali-G610", language: str = "glsl") -> dict:
    """Compile one shader with malioc and return a condensed report.

    language "glsl": source is GLSL text. "spirv": source is a base64 SPIR-V binary (Vulkan); malioc picks
    the input type from the .frag.spv / .vert.spv / .comp.spv suffix."""
    ext = {"pixel": ".frag", "vertex": ".vert", "compute": ".comp"}.get(stage, ".frag")
    if language == "spirv":
        ext += ".spv"
    fd, path = tempfile.mkstemp(suffix=ext, prefix="rdmcp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(source) if language == "spirv" else source.encode("utf-8"))
        out = subprocess.run(
            [find_malioc(), "-c", gpu, "--format", "json", path],
            capture_output=True, text=True, timeout=120,
        )
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            raise MaliocError((out.stdout or out.stderr or "empty output")[-800:])
        if not data.get("shaders"):
            raise MaliocError("malioc returned no shader report: %s" % (out.stderr or out.stdout)[-800:])
        return _summarize(data)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def analyze_many(sources: dict[str, str], stage: str = "pixel", gpu: str = "Mali-G610",
                 workers: int = 8, language: str = "glsl") -> dict[str, dict]:
    """Compile several sources in parallel; failures are reported per source"""

    def run(item):
        key, src = item
        try:
            return key, analyze_source(src, stage, gpu, language)
        except Exception as e:  # keep the batch going
            return key, {"error": str(e)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(run, sources.items()))
