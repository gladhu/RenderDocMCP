"""
GPU performance counters of the replay device.

EnumerateCounters lists what the device (a phone, for remote replay) offers: the generic counters of
RenderDoc's GPUCounter enum (GPU duration, samples passed, vertex / fragment invocations, ...) and, where the
driver exposes them, vendor counters (Arm Mali: ids from 5,000,000, Vulkan extended: from 4,000,000). The set
differs per device, driver and API, so the tool lists before it fetches and says what is missing.

Values are read per event; a CounterValue holds the number in the field matching the counter's result type
and byte width.
"""

import collections

import renderdoc as rd

UNIT_NAMES = {0: "", 1: "s", 2: "%", 3: "ratio", 4: "bytes", 5: "cycles", 6: "Hz", 7: "V", 8: "C"}
COMP_FLOAT, COMP_UINT, COMP_SINT = "float", "uint", "sint"
NAMED = {            # generic counters by short name, for convenience
    "gpu_duration": 1, "input_vertices_read": 2, "ia_primitives": 3, "gs_primitives": 4,
    "rasterizer_invocations": 5, "rasterized_primitives": 6, "samples_passed": 7, "vs_invocations": 8,
    "hs_invocations": 9, "ds_invocations": 10, "gs_invocations": 11, "ps_invocations": 12,
    "cs_invocations": 13, "as_invocations": 14, "ms_invocations": 15,
}
GENERIC_MAX = 100          # ids below this are RenderDoc's generic counters


# ---------------------------------------------------------------- pure helpers

def vendor_of(counter_id):
    """"generic" / "amd" / "intel" / "nvidia" / "vulkan_ext" / "arm" for a counter id"""
    c = int(counter_id)
    if c < GENERIC_MAX:
        return "generic"
    for lo, hi, name in ((1000000, 1999999, "amd"), (2000000, 2999999, "intel"), (3000000, 3999999, "nvidia"),
                         (4000000, 4999999, "vulkan_ext"), (5000000, 6000000, "arm")):
        if lo <= c <= hi:
            return name
    return "other"


def pick_value(value, result_type, byte_width):
    """Number out of a CounterValue (an object with d / f / u32 / u64) by result type and width"""
    if result_type == COMP_FLOAT:
        return float(value.d if byte_width == 8 else value.f)
    return int(value.u64 if byte_width == 8 else value.u32)


def resolve_counters(selectors, available):
    """
    Turn user selectors into counter ids. A selector is a short name (gpu_duration, samples_passed, ...), a
    numeric id, a group ("@arm", "@generic", "@all"), a counter name (exact, case-insensitive), or a substring
    of counter names when no name matches exactly. available: {id: description dict}.
    Returns (ids, unknown_selectors).
    """
    ids, unknown = [], []
    for sel in selectors:
        text = str(sel).strip()
        low = text.lower()
        if low.startswith("@"):
            group = low[1:]
            found = sorted(i for i, d in available.items() if group == "all" or d["vendor"] == group)
        elif low in NAMED:
            found = [NAMED[low]]
        elif text.isdigit():
            found = [int(text)]
        else:
            exact = sorted(i for i, d in available.items() if d["name"].strip().lower() == low)
            found = exact or sorted(i for i, d in available.items() if low in d["name"].lower())
        found = [i for i in found if i in available]
        if not found:
            unknown.append(text)
        for i in found:
            if i not in ids:
                ids.append(i)
    return ids, unknown


def _r(x):
    """6 significant digits: GPU durations are ~1e-5 s, fixed decimals would wipe them out"""
    return float("%.6g" % x) if isinstance(x, float) else x


def median(values):
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def combine_runs(runs):
    """
    runs: [[(event_id, counter_id, value)]], one list per measurement. Returns (rows, run_totals):
    rows holds the per (event, counter) median over the runs that measured it, run_totals {counter: [sum per run]}.
    """
    totals = collections.defaultdict(list)
    cells = collections.OrderedDict()
    for rows in runs:
        run_sum = collections.defaultdict(float)
        for eid, cid, val in rows:
            cells.setdefault((eid, cid), []).append(val)
            run_sum[cid] += val
        for cid, v in run_sum.items():
            totals[cid].append(v)
    merged = [(eid, cid, median(vals)) for (eid, cid), vals in cells.items()]
    return merged, dict(totals)


def spread_pct(run_totals):
    """(max - min) / median of the per-run totals, in percent; None with fewer than two runs"""
    if len(run_totals) < 2:
        return None
    m = median(run_totals)
    return round(100.0 * (max(run_totals) - min(run_totals)) / m, 2) if m else None


def aggregate(rows, counter_ids, descs, top_n=10, names=None):
    """
    rows: [(event_id, counter_id, value)]. Per counter: total over events, event count, max and the top_n
    events. Counters that are not additive (percent, ratio, Hz, temperature) get no total.
    """
    names = names or {}
    per = collections.OrderedDict((c, []) for c in counter_ids)
    for eid, cid, val in rows:
        if cid in per:
            per[cid].append((eid, val))
    out = []
    for cid, vals in per.items():
        d = descs[cid]
        additive = d["unit"] in ("", "s", "bytes", "cycles")
        vals.sort(key=lambda ev: -ev[1])
        entry = {
            "counter": cid, "name": d["name"], "unit": d["unit"], "category": d["category"],
            "events": len(vals),
            "max": _r(vals[0][1]) if vals else None,
            "min": _r(min((v for _, v in vals), default=None)),
            "mean": _r(sum(v for _, v in vals) / len(vals)) if vals else None,
            "top_events": [{"event_id": e, "name": names.get(e, ""), "value": _r(v)} for e, v in vals[: max(1, int(top_n))]],
        }
        if additive and vals:
            entry["total"] = _r(sum(v for _, v in vals))
        out.append(entry)
    return out


# ---------------------------------------------------------------- service

class CounterService:
    """GPU performance counters"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_gpu_counters(self, counters=None, list_only=False, event_ids=None, marker_filter=None,
                         event_id_min=None, event_id_max=None, top_n=10, include_events=False, repeat=1):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        repeat = max(1, min(int(repeat or 1), 10))
        result = {"data": None, "error": None}

        def callback(controller):
            try:
                run(controller)
            except Exception as e:                              # BlockInvoke swallows exceptions
                result["error"] = "%s: %s" % (type(e).__name__, e)

        def run(controller):
            available = {}
            for c in controller.EnumerateCounters():
                d = controller.DescribeCounter(c)
                available[int(c)] = {
                    "name": str(d.name), "category": str(d.category), "description": str(d.description),
                    "unit": UNIT_NAMES.get(int(d.unit), str(int(d.unit))),
                    "result": COMP_FLOAT if int(d.resultType) == int(rd.CompType.Float) else (
                        COMP_SINT if int(d.resultType) == int(rd.CompType.SInt) else COMP_UINT),
                    "bytes": int(d.resultByteWidth), "vendor": vendor_of(int(c)),
                }
            catalogue = [dict(id=i, **d) for i, d in sorted(available.items())]
            if list_only or not counters:
                result["data"] = {"available_count": len(catalogue), "available": catalogue,
                                  "by_vendor": dict(collections.Counter(c["vendor"] for c in catalogue))}
                return
            ids, unknown = resolve_counters(counters, available)
            if not ids:
                raise ValueError("None of %s is available on this device. Available: %s" % (
                    list(counters), ", ".join("%s(%d)" % (d["name"], i) for i, d in sorted(available.items()))[:900]))
            wanted = set(int(e) for e in event_ids) if event_ids else None
            sf = controller.GetStructuredFile()
            names, in_scope = {}, set()

            def walk(actions, path):
                for a in actions:
                    name = a.GetName(sf)
                    kids = a.children
                    eid = int(a.eventId)
                    if kids:
                        walk(kids, path + [name])
                    if a.flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch):
                        if wanted is not None and eid not in wanted:
                            continue
                        if marker_filter and marker_filter.lower() not in "/".join(path).lower():
                            continue
                        if event_id_min is not None and eid < int(event_id_min):
                            continue
                        if event_id_max is not None and eid > int(event_id_max):
                            continue
                        names[eid] = name
                        in_scope.add(eid)

            walk(controller.GetRootActions(), [])
            runs = []
            for _ in range(repeat):
                rows = []
                for r in controller.FetchCounters([rd.GPUCounter(i) if i < GENERIC_MAX else i for i in ids]):
                    cid, eid = int(r.counter), int(r.eventId)
                    if eid in in_scope and cid in available:
                        rows.append((eid, cid, pick_value(r.value, available[cid]["result"], available[cid]["bytes"])))
                runs.append(rows)
            rows, run_totals = combine_runs(runs)
            fetched = aggregate(rows, ids, available, top_n, names)
            data = {"events_in_scope": len(in_scope), "counters": fetched, "repeat": repeat,
                    "unknown_selectors": unknown, "values_returned": len(rows)}
            if repeat > 1:
                for entry in fetched:
                    totals = run_totals.get(entry["counter"], [])
                    entry["run_totals"] = [_r(t) for t in totals]
                    entry["run_spread_pct"] = spread_pct(totals)
                data["note"] = "per-event values are the median of %d measurements; run_spread_pct is (max-min)/median of the per-run totals" % repeat
            if include_events:
                per_event = collections.OrderedDict()
                for eid, cid, val in sorted(rows):
                    per_event.setdefault(eid, {"event_id": eid, "name": names.get(eid, "")})[available[cid]["name"].strip() or str(cid)] = _r(val)
                data["events"] = list(per_event.values())
            result["data"] = data

        self._invoke(callback)
        if result["error"]:
            raise ValueError(result["error"])
        return result["data"]
