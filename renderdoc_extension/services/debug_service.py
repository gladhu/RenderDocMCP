"""
API debug / validation messages of the capture.

RenderDoc collects the driver's debug output while the frame is captured (KHR_debug on GL/GLES, validation
layers on Vulkan, the D3D debug layer) and adds its own analysis messages during replay. The same message
is usually repeated hundreds of times, so the result is grouped by (message id, text) with a count and a few
sample events, and the flat list is capped.
"""

SEVERITY_NAMES = {0: "high", 1: "medium", 2: "low", 3: "info"}
CATEGORY_NAMES = {
    0: "Application_Defined", 1: "Miscellaneous", 2: "Initialization", 3: "Cleanup", 4: "Compilation",
    5: "State_Creation", 6: "State_Setting", 7: "State_Getting", 8: "Resource_Manipulation", 9: "Execution",
    10: "Shaders", 11: "Deprecated", 12: "Undefined", 13: "Portability", 14: "Performance",
}
SOURCE_NAMES = {
    0: "API", 1: "RedundantAPIUse", 2: "IncorrectAPIUse", 3: "GeneralPerformance", 4: "GCNPerformance",
    5: "RuntimeWarning", 6: "UnsupportedConfiguration",
}
SEVERITY_RANK = dict((name, rank) for rank, name in SEVERITY_NAMES.items())     # high = 0 ... info = 3


def _name(table, value):
    try:
        return table.get(int(value), str(int(value)))
    except Exception:
        text = str(value)
        return text.rsplit(".", 1)[-1]


def normalise(msg):
    """DebugMessage -> plain dict"""
    return {
        "event_id": int(msg.eventId),
        "severity": _name(SEVERITY_NAMES, msg.severity),
        "category": _name(CATEGORY_NAMES, msg.category),
        "source": _name(SOURCE_NAMES, msg.source),
        "message_id": int(msg.messageID),
        "description": str(msg.description),
    }


def summarise(messages, min_severity="info", category=None, source=None, text_filter=None,
              event_id_min=None, event_id_max=None, max_messages=100, max_groups=30):
    """
    Filter and group normalised messages.

    min_severity: keep messages at least this severe ("high" > "medium" > "low" > "info").
    category / source: exact name, or a case-insensitive substring of it.
    text_filter: case-insensitive substring of the description.
    Counts (by_severity, by_category, by_source, total_matched) cover every matching message; the groups
    and the flat list are capped (max_groups, max_messages) and say so in groups_truncated / truncated.
    """
    if min_severity not in SEVERITY_RANK:
        raise ValueError("min_severity must be one of: %s" % ", ".join(sorted(SEVERITY_RANK, key=SEVERITY_RANK.get)))
    limit = SEVERITY_RANK[min_severity]
    category = category.lower() if category else None
    source = source.lower() if source else None
    text = text_filter.lower() if text_filter else None

    matched = []
    for m in messages:
        if SEVERITY_RANK.get(m["severity"], 3) > limit:
            continue
        if category and category not in m["category"].lower():
            continue
        if source and source not in m["source"].lower():
            continue
        if text and text not in m["description"].lower():
            continue
        if event_id_min is not None and m["event_id"] < event_id_min:
            continue
        if event_id_max is not None and m["event_id"] > event_id_max:
            continue
        matched.append(m)

    def count_by(key):
        out = {}
        for m in matched:
            out[m[key]] = out.get(m[key], 0) + 1
        return out

    groups = {}
    for m in matched:
        key = (m["message_id"], m["description"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "severity": m["severity"], "category": m["category"], "source": m["source"],
                "message_id": m["message_id"], "description": m["description"],
                "count": 0, "first_event_id": m["event_id"], "last_event_id": m["event_id"],
                "sample_event_ids": [],
            }
        g["count"] += 1
        g["first_event_id"] = min(g["first_event_id"], m["event_id"])
        g["last_event_id"] = max(g["last_event_id"], m["event_id"])
        if len(g["sample_event_ids"]) < 5 and m["event_id"] not in g["sample_event_ids"]:
            g["sample_event_ids"].append(m["event_id"])
    ranked = sorted(groups.values(), key=lambda g: (SEVERITY_RANK.get(g["severity"], 3), -g["count"], g["first_event_id"]))

    ordered = sorted(matched, key=lambda m: (SEVERITY_RANK.get(m["severity"], 3), m["event_id"]))
    return {
        "total_matched": len(matched),
        "by_severity": count_by("severity"),
        "by_category": count_by("category"),
        "by_source": count_by("source"),
        "group_count": len(ranked),
        "groups_truncated": len(ranked) > max_groups,
        "groups": ranked[:max_groups],
        "truncated": len(ordered) > max_messages,
        "messages": ordered[:max_messages],
    }


class DebugService:
    """Debug / validation messages"""

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    def get_debug_messages(self, min_severity="info", category=None, source=None, text_filter=None,
                           event_id_min=None, event_id_max=None, max_messages=100, max_groups=30):
        """Debug messages of the loaded capture (UI-side list; no replay-thread access needed)"""
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        messages = [normalise(m) for m in self.ctx.DebugMessages()]
        result = summarise(messages, min_severity, category, source, text_filter,
                           event_id_min, event_id_max, max(0, int(max_messages)), max(1, int(max_groups)))
        result["total_in_capture"] = len(messages)
        return result
