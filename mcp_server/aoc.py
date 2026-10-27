"""
Adreno Offline Compiler (aoc) wrapper.

Runs in the MCP server process (full Python), not inside RenderDoc, for the same
reason as malioc.py. aoc has no JSON output and no cycle model: it reports
instruction counts per class, register footprint and ALU fiber occupancy as text.
"""

import glob
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor


_CANDIDATES = [
    r"C:\Qualcomm\**\aoc.exe",
    r"D:\Qualcomm\**\aoc.exe",
]

_STAGE_EXT = {"pixel": ".frag", "vertex": ".vert", "compute": ".comp"}

_SECTION = re.compile(r"={3,}\s*Shader Stats\s+(.+?)\s*={3,}")
_STAT = re.compile(r"^(.+?)\s*:\s*(-?\d+(?:\.\d+)?)\s*$", re.MULTILINE)

# aoc stat label (normalized) -> condensed report key
_SUMMARY_KEYS = {
    "total_instruction_count": "total_instructions",
    "alu_instruction_count_32_bit": "alu32",
    "alu_instruction_count_16_bit": "alu16",
    "complex_instruction_count_32_bit": "complex32",
    "complex_instruction_count_16_bit": "complex16",
    "flow_control_instruction_count": "flow_control",
    "texture_read_instruction_count": "texture_reads",
    "memory_read_instruction_count": "memory_reads",
    "memory_write_instruction_count": "memory_writes",
    "short_latency_sync_instruction_count": "short_sync",
    "long_latency_sync_instruction_count": "long_sync",
    "full_precision_register_footprint_per_shader_instance": "full_registers",
    "half_precision_register_footprint_per_shader_instance": "half_registers",
    "overall_register_footprint_per_shader_instance": "registers",
    "scratch_memory_usage_per_shader_instance": "scratch_bytes",
    "loop_count": "loops",
    "alu_fiber_occupancy_percentage": "fiber_occupancy",
}


class AocError(Exception):
    """aoc not found or failed"""

    pass


def find_aoc() -> str:
    """AOC_PATH env > aoc on PATH > C:\\Qualcomm install dirs"""
    env = os.environ.get("AOC_PATH")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("aoc")
    if found:
        return found
    for pattern in _CANDIDATES:
        hits = sorted(glob.glob(pattern, recursive=True), reverse=True)
        if hits:
            return hits[0]
    raise AocError(
        "aoc not found. Install the Qualcomm Adreno SDK offline compiler or set AOC_PATH to aoc.exe"
    )


def aoc_version() -> str:
    # aoc has no --version; the banner is printed even when no file is given
    out = subprocess.run([find_aoc(), "-h"], capture_output=True, text=True, timeout=30)
    m = re.search(r"AOC Version\s*:\s*(\S+)", out.stdout)
    return m.group(1) if m else "unknown"


def _normalize(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def _parse_stats(body: str) -> dict:
    stats = {}
    for label, value in _STAT.findall(body):
        stats[_normalize(label)] = float(value) if "." in value else int(value)
    return stats


def _summarize(stdout: str, arch: str) -> dict:
    if "Compilation succeeded" not in stdout:
        raise AocError(_error_text(stdout))

    parts = _SECTION.split(stdout)  # [preamble, name, body, name, body, ...]
    sections = {parts[i].strip(): _parse_stats(parts[i + 1]) for i in range(1, len(parts) - 1, 2)}
    if not sections:
        raise AocError("aoc returned no shader stats: %s" % stdout[-800:])

    # First section is the shader itself; later ones are extra variants (e.g. binning VS)
    name, stats = next(iter(sections.items()))
    report = {"arch": arch, "section": name}
    for src, dst in _SUMMARY_KEYS.items():
        if src in stats:
            report[dst] = stats[src]

    alu = report.get("alu32", 0) + report.get("alu16", 0)
    if alu:
        report["fp16_arithmetic"] = round(100.0 * report.get("alu16", 0) / alu, 1)
    report["stack_spilling"] = report.get("scratch_bytes", 0) > 0
    # aoc exposes no cycle model: total instruction count is the cost proxy
    report["cost"] = report.get("total_instructions")
    if len(sections) > 1:
        report["other_sections"] = {k: v for k, v in sections.items() if k != name}
    return report


def _error_text(stdout: str) -> str:
    lines = [l.strip() for l in stdout.splitlines() if l.strip().startswith("ERROR")]
    return "\n".join(lines[:10]) if lines else (stdout.strip()[-800:] or "empty output")


def analyze_source(source: str, stage: str = "pixel", arch: str = "a650") -> dict:
    """Compile one GLSL source with aoc and return a condensed report"""
    ext = _STAGE_EXT.get(stage, ".frag")
    fd, path = tempfile.mkstemp(suffix=ext, prefix="rdmcp_aoc_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(source)
        out = subprocess.run(
            [find_aoc(), "-arch=%s" % arch, path],
            capture_output=True, text=True, timeout=120,
        )
        return _summarize(out.stdout + out.stderr, arch)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def analyze_many(sources: dict[str, str], stage: str = "pixel", arch: str = "a650",
                 workers: int = 8) -> dict[str, dict]:
    """Compile several sources in parallel; failures are reported per source"""

    def run(item):
        key, src = item
        try:
            return key, analyze_source(src, stage, arch)
        except Exception as e:  # keep the batch going
            return key, {"error": str(e)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(run, sources.items()))
