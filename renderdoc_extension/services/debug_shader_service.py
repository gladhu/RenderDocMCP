"""
Shader debugging: run one pixel / vertex / compute invocation of the shader bound at an event and record every step.

RenderDoc emulates the invocation (Vulkan SPIR-V and D3D; not OpenGL / GLES) with the real inputs, constants and
textures of the event, so the values are what the shader computed for that pixel. A trace can be thousands of steps,
so the result is a summary (steps, hot source lines, the first step that generated a NaN / Inf, the final values of
the variables) plus a bounded list of steps that holds only the variables each step changed.
"""

import collections
import math

import renderdoc as rd

from ..utils import Helpers

UNINIT_BITS = 0xCCCCCCCC            # what the debugger fills a variable with before its first write
NO_PREFERENCE = 0xFFFFFFFF            # ReplayController.NoPreference
EVENT_FLAGS = {1: "sample_load_gather", 2: "generated_nan_or_inf", 4: "debug_break"}
SUPPORTED_APIS = ("Vulkan", "D3D11", "D3D12")


# ---------------------------------------------------------------- pure helpers

def event_flag_names(flags):
    v = int(flags)
    return [name for bit, name in sorted(EVENT_FLAGS.items()) if v & bit]


def clean_number(x):
    """JSON-safe: inf / nan become strings, floats are shortened"""
    if isinstance(x, float):
        if math.isnan(x):
            return "nan"
        if math.isinf(x):
            return "inf" if x > 0 else "-inf"
        return float("%.7g" % x)
    return x


def component_values(value, var_type, count):
    """
    First `count` components of a ShaderValue as plain numbers, picked by variable type name. A 32-bit component
    that still holds the debugger's fill pattern (never written) is the string "uninit", not a number.
    """
    kind = (var_type or "Float").lower()
    if kind in ("float", "half", "sint", "uint", "sshort", "ushort", "sbyte", "ubyte", "bool", "enum"):
        bits = list(value.u32v)[:count]
        vals = _typed_values(value, kind, count)
        return ["uninit" if i < len(bits) and bits[i] == UNINIT_BITS else v for i, v in enumerate(vals)]
    return _typed_values(value, kind, count)


def _typed_values(value, kind, count):
    if kind in ("float", "half"):
        raw = value.f32v
    elif kind == "double":
        raw = value.f64v
    elif kind in ("sint", "sshort", "sbyte"):
        raw = value.s32v
    elif kind in ("slong",):
        raw = value.s64v
    elif kind in ("ulong",):
        raw = value.u64v
    elif kind == "bool":
        return [bool(v) for v in value.u32v[:count]]
    else:
        raw = value.u32v
    return [clean_number(v) for v in list(raw)[:count]]


def describe_variable(var, type_name):
    """ShaderVariable -> {"name", "type", "rows", "columns", "value": scalar / list / list of rows}"""
    rows, cols = max(1, int(var.rows)), max(1, int(var.columns))
    flat = component_values(var.value, type_name, rows * cols)
    out = {"name": str(var.name), "type": type_name}
    if rows > 1:
        out["rows"], out["columns"] = rows, cols
        out["value"] = [flat[r * cols:(r + 1) * cols] for r in range(rows)]
    elif cols > 1:
        out["value"] = flat
    else:
        out["value"] = flat[0] if flat else None
    return out


def is_uninitialised(x):
    """True when a value (scalar or nested list) has no initialised component at all"""
    if isinstance(x, (list, tuple)):
        return bool(x) and all(is_uninitialised(v) for v in x)
    return x == "uninit"


def contains_bad_number(x):
    """True when a value (scalar or nested list) holds a NaN / Inf marker produced by clean_number"""
    if isinstance(x, (list, tuple)):
        return any(contains_bad_number(v) for v in x)
    return x in ("nan", "inf", "-inf")


def summarise_trace(steps, max_steps=60, max_lines=12):
    """
    steps: [{"step", "instruction", "line", "flags": [..], "changes": [{"name", "value"}]}]
    Returns the bounded step list plus: the first step that produced a NaN / Inf (by flag or by value), source lines
    ranked by the number of steps spent on them, and the last value written to every variable.
    """
    first_bad = None
    lines = collections.Counter()
    last = collections.OrderedDict()
    for s in steps:
        if s.get("line") is not None:
            lines[s["line"]] += 1
        for c in s["changes"]:
            last[c["name"]] = {"value": c["value"], "step": s["step"]}
            if first_bad is None and contains_bad_number(c["value"]):
                first_bad = {"step": s["step"], "line": s.get("line"), "variable": c["name"], "value": c["value"]}
        if first_bad is None and "generated_nan_or_inf" in s["flags"]:
            first_bad = {"step": s["step"], "line": s.get("line"), "variable": None, "value": None}
    shown = steps[:max_steps]
    return {
        "total_steps": len(steps),
        "steps_shown": len(shown),
        "steps_truncated": len(steps) > len(shown),
        "steps": shown,
        "first_nan_or_inf": first_bad,
        "nan_at_end": [name for name, v in last.items() if contains_bad_number(v["value"])][:40],
        "hot_lines": [{"line": ln, "steps": n} for ln, n in lines.most_common(max_lines)],
        "final_values": [dict(name=k, **v) for k, v in list(last.items())[-40:]],
    }


# ---------------------------------------------------------------- service

class DebugShaderService:
    """Step-by-step shader invocation traces"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def debug_shader(self, event_id, kind="pixel", x=None, y=None, vertex=None, instance=0, index=None,
                     group=None, thread=None, primitive=None, sample=0, max_steps=60, only_changes_of=None,
                     max_total_steps=20000):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        kind = (kind or "pixel").lower()
        if kind not in ("pixel", "vertex", "compute"):
            raise ValueError("kind must be pixel, vertex or compute")
        if kind == "pixel" and (x is None or y is None):
            raise ValueError("a pixel trace needs x and y")
        if kind == "vertex" and vertex is None:
            raise ValueError("a vertex trace needs vertex (the vertex id within the draw)")
        wanted = [w.lower() for w in (only_changes_of or [])]
        result = {"data": None}

        def callback(controller):
            api = Helpers.enum_name("GraphicsAPI", Helpers.api_props(controller).pipelineType)
            if api not in SUPPORTED_APIS:
                raise ValueError("shader debugging is not supported on %s (only Vulkan and D3D). For GL / GLES use "
                                 "replace_shader to print values, or pixel_history / diff_textures" % api)
            controller.SetFrameEvent(int(event_id), True)
            if kind == "pixel":
                inputs = rd.DebugPixelInputs()
                inputs.sample = int(sample or 0)
                inputs.primitive = NO_PREFERENCE if primitive is None else int(primitive)
                inputs.view = NO_PREFERENCE
                trace = controller.DebugPixel(int(x), int(y), inputs)
                where = {"x": int(x), "y": int(y), "sample": int(sample or 0)}
            elif kind == "vertex":
                idx = int(index) if index is not None else int(vertex)
                trace = controller.DebugVertex(int(vertex), int(instance or 0), idx, 0)
                where = {"vertex": int(vertex), "instance": int(instance or 0), "index": idx}
            else:
                g = tuple(int(v) for v in (group or (0, 0, 0)))
                t = tuple(int(v) for v in (thread or (0, 0, 0)))
                trace = controller.DebugThread(g, t)
                where = {"group": list(g), "thread": list(t)}
            if trace is None or trace.debugger is None:
                raise ValueError("no trace: the %s did not run at %s (a pixel that no fragment of this draw wrote, "
                                 "or the shader debugger is not available for this draw)" % (kind, where))
            try:
                steps, vars_info = self._collect(controller, trace, wanted, int(max_total_steps))
                executed = steps["executed"]
                data = summarise_trace(steps["steps"], int(max_steps))
                data["executed_steps"] = executed
                if wanted:
                    data["filtered_to"] = list(only_changes_of)
                data.update({
                    "event_id": int(event_id), "kind": kind, "where": where, "api": api,
                    "stage": Helpers.enum_name("ShaderStage", trace.stage),
                    "inputs": vars_info["inputs"], "constant_blocks": vars_info["constants"],
                    "run_complete": steps["complete"],
                })
                result["data"] = data
            finally:
                controller.FreeTrace(trace)

        self._invoke(callback)
        return result["data"]

    def _collect(self, controller, trace, wanted, max_total):
        """Run the trace to the end; returns ({"steps": [...], "complete"}, {"inputs", "constants"})"""
        lines = {}
        for ii in trace.instInfo:
            try:
                li = ii.lineInfo
                lines[int(ii.instruction)] = int(li.lineStart) if int(li.lineStart) >= 0 else None
            except Exception:
                pass
        steps, complete, executed = [], False, 0
        while executed < max_total:
            states = controller.ContinueDebug(trace.debugger)
            if not states:
                complete = True
                break
            for st in states:
                executed += 1
                changes, declared = [], 0
                for ch in st.changes:
                    after = ch.after
                    tname = Helpers.enum_name("VarType", after.type)
                    d = describe_variable(after, tname)
                    if wanted and not any(w in d["name"].lower() for w in wanted):
                        continue
                    if is_uninitialised(d["value"]):
                        declared += 1                    # the debugger declaring a variable, not the shader writing it
                        continue
                    changes.append({"name": d["name"], "value": d["value"]})
                if wanted and not changes and not event_flag_names(st.flags):
                    continue                                 # following selected variables: skip steps that touch none
                steps.append({
                    "step": int(st.stepIndex), "instruction": int(st.nextInstruction),
                    "line": lines.get(int(st.nextInstruction)),
                    "flags": event_flag_names(st.flags), "changes": changes,
                })
                if declared:
                    steps[-1]["uninitialised_declared"] = declared
                if executed >= max_total:
                    break
        if len(set(s["line"] for s in steps)) <= 1:          # no debug info: every step says line 0, which tells nothing
            for s in steps:
                s["line"] = None
        info = {
            "inputs": [describe_variable(v, Helpers.enum_name("VarType", v.type)) for v in trace.inputs][:40],
            "constants": [{"name": str(cb.name), "members": len(cb.members)} for cb in trace.constantBlocks][:20],
        }
        return {"steps": steps, "complete": complete, "executed": executed}, info
