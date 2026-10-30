"""
Vulkan barrier / layout analysis from the recorded vkCmdPipelineBarrier calls.

Pure functions over data the bridge already provides (get_api_calls, the action list, resource usage), so the rules
are unit-testable without a device. What it does NOT do: it does not simulate layouts through render passes (render
pass initial / final layouts change layouts implicitly), it does not know which stage / access pairs are legal (the
validation layers do: see get_debug_messages), and it only parses the legacy vkCmdPipelineBarrier - the synchronization2
and event calls are counted and reported as not analysed.

Decoded values follow the Vulkan spec. VkPipelineStageFlagBits2 / VkAccessFlagBits2 share their low bits with the
legacy flags, which is what matters for the names below.
"""

import collections

STAGES = {
    0x1: "TOP_OF_PIPE", 0x2: "DRAW_INDIRECT", 0x4: "VERTEX_INPUT", 0x8: "VERTEX_SHADER",
    0x10: "TESSELLATION_CONTROL", 0x20: "TESSELLATION_EVALUATION", 0x40: "GEOMETRY_SHADER",
    0x80: "FRAGMENT_SHADER", 0x100: "EARLY_FRAGMENT_TESTS", 0x200: "LATE_FRAGMENT_TESTS",
    0x400: "COLOR_ATTACHMENT_OUTPUT", 0x800: "COMPUTE_SHADER", 0x1000: "TRANSFER", 0x2000: "BOTTOM_OF_PIPE",
    0x4000: "HOST", 0x8000: "ALL_GRAPHICS", 0x10000: "ALL_COMMANDS",
}
ACCESS = {
    0x1: "INDIRECT_COMMAND_READ", 0x2: "INDEX_READ", 0x4: "VERTEX_ATTRIBUTE_READ", 0x8: "UNIFORM_READ",
    0x10: "INPUT_ATTACHMENT_READ", 0x20: "SHADER_READ", 0x40: "SHADER_WRITE", 0x80: "COLOR_ATTACHMENT_READ",
    0x100: "COLOR_ATTACHMENT_WRITE", 0x200: "DEPTH_STENCIL_ATTACHMENT_READ", 0x400: "DEPTH_STENCIL_ATTACHMENT_WRITE",
    0x800: "TRANSFER_READ", 0x1000: "TRANSFER_WRITE", 0x2000: "HOST_READ", 0x4000: "HOST_WRITE",
    0x8000: "MEMORY_READ", 0x10000: "MEMORY_WRITE",
}
LAYOUTS = {
    0: "UNDEFINED", 1: "GENERAL", 2: "COLOR_ATTACHMENT_OPTIMAL", 3: "DEPTH_STENCIL_ATTACHMENT_OPTIMAL",
    4: "DEPTH_STENCIL_READ_ONLY_OPTIMAL", 5: "SHADER_READ_ONLY_OPTIMAL", 6: "TRANSFER_SRC_OPTIMAL",
    7: "TRANSFER_DST_OPTIMAL", 8: "PREINITIALIZED", 1000001002: "PRESENT_SRC_KHR",
}
ASPECTS = {0x1: "color", 0x2: "depth", 0x4: "stencil", 0x8: "metadata"}
BROAD = (0x8000, 0x10000)                       # ALL_GRAPHICS, ALL_COMMANDS
UNDEFINED = 0
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}
THRASH_TRANSITIONS = 4


# ---------------------------------------------------------------- decoding

def decode_flags(value, table):
    """Names of the set bits; bits the table does not know are appended as hex"""
    v = int(value)
    names = [name for bit, name in sorted(table.items()) if v & bit]
    known = 0
    for bit in table:
        known |= bit
    if v & ~known:
        names.append("0x%x" % (v & ~known))
    return names


def layout_name(value):
    return LAYOUTS.get(int(value), "LAYOUT_%d" % int(value))


def params_of(call):
    return dict((p["name"], p["value"]) for p in call.get("params", []))


def decode_barrier(call):
    """vkCmdPipelineBarrier call (from get_api_calls) -> a plain barrier dict, or None for other calls"""
    if call.get("name") != "vkCmdPipelineBarrier":
        return None
    p = params_of(call)
    src, dst = int(p.get("srcStageMask", 0)), int(p.get("destStageMask", 0))
    images = []
    for b in p.get("pImageMemoryBarriers") or []:
        rng = b.get("subresourceRange") or {}
        old, new = int(b.get("oldLayout", 0)), int(b.get("newLayout", 0))
        qs, qd = int(b.get("srcQueueFamilyIndex", -1)), int(b.get("dstQueueFamilyIndex", -1))
        images.append({
            "image": str(b.get("image")), "old_layout": old, "new_layout": new,
            "old": layout_name(old), "new": layout_name(new),
            "src_access": decode_flags(b.get("srcAccessMask", 0), ACCESS),
            "dst_access": decode_flags(b.get("dstAccessMask", 0), ACCESS),
            "aspects": decode_flags(rng.get("aspectMask", 0), ASPECTS),
            "mips": int(rng.get("levelCount", 1)), "layers": int(rng.get("layerCount", 1)),
            "queue_transfer": qs != qd and qs != -1 and qd != -1,
        })
    buffers = [{"buffer": str(b.get("buffer")), "src_access": decode_flags(b.get("srcAccessMask", 0), ACCESS),
                "dst_access": decode_flags(b.get("dstAccessMask", 0), ACCESS)}
               for b in p.get("pBufferMemoryBarriers") or []]
    return {
        "event_id": int(call["event_id"]), "command_buffer": str(p.get("commandBuffer")),
        "src_stage_mask": src, "dst_stage_mask": dst,
        "src_stages": decode_flags(src, STAGES), "dst_stages": decode_flags(dst, STAGES),
        "memory_barriers": len(p.get("pMemoryBarriers") or []),
        "buffer_barriers": buffers, "image_barriers": images,
    }


# ---------------------------------------------------------------- rules

def issue(kind, severity, event_ids, text, **extra):
    d = {"type": kind, "severity": severity, "event_ids": sorted(set(event_ids))[:20], "count": len(set(event_ids)), "description": text}
    d.update(extra)
    return d


def find_broad_masks(barriers):
    hits = [b["event_id"] for b in barriers if b["src_stage_mask"] & sum(BROAD) or b["dst_stage_mask"] & sum(BROAD)]
    if not hits:
        return None
    return issue("broad_stage_mask", "medium", hits,
                 "ALL_COMMANDS / ALL_GRAPHICS in a stage mask: the barrier waits for (or blocks) every stage, which "
                 "serialises vertex and fragment work that could overlap - most costly on tile-based GPUs. Name the "
                 "stages that really produce and consume the data.")


def find_mergeable(barriers, work_events):
    """Consecutive barriers of one command buffer with no draw / dispatch / copy / clear / pass boundary between them"""
    work = sorted(work_events)
    runs, cur = [], []

    def work_between(a, b):
        import bisect
        i = bisect.bisect_right(work, a)
        return i < len(work) and work[i] < b

    for b in sorted(barriers, key=lambda x: x["event_id"]):
        if cur and cur[-1]["command_buffer"] == b["command_buffer"] and not work_between(cur[-1]["event_id"], b["event_id"]):
            cur.append(b)
        else:
            if len(cur) > 1:
                runs.append(cur)
            cur = [b]
    if len(cur) > 1:
        runs.append(cur)
    if not runs:
        return None
    saved = sum(len(r) - 1 for r in runs)
    return issue("mergeable_barriers", "low", [b["event_id"] for r in runs for b in r],
                 "%d barrier(s) directly follow another barrier of the same command buffer with no work in between: "
                 "they could be recorded as one vkCmdPipelineBarrier with all the image / buffer barriers." % saved,
                 groups=[[b["event_id"] for b in r] for r in runs[:20]], barriers_saved=saved)


def transitions_by_image(barriers):
    """{image: [(event, old, new)]} for real layout changes"""
    out = collections.OrderedDict()
    for b in sorted(barriers, key=lambda x: x["event_id"]):
        for i in b["image_barriers"]:
            if i["old_layout"] != i["new_layout"]:
                out.setdefault(i["image"], []).append((b["event_id"], i["old_layout"], i["new_layout"]))
    return out


def find_thrash(barriers, names=None):
    names = names or {}
    bad = [(img, t) for img, t in transitions_by_image(barriers).items() if len(t) >= THRASH_TRANSITIONS]
    if not bad:
        return None
    return issue("layout_thrash", "low", [e for _, t in bad for e, _, _ in t],
                 "An image changes layout %d or more times in one frame; each change can force a decompress / flush on "
                 "the GPU. Group the uses that need the same layout." % THRASH_TRANSITIONS,
                 images=[{"image": img, "name": names.get(img, ""), "transitions": len(t),
                          "sequence": ["%s -> %s @%d" % (layout_name(o), layout_name(n), e) for e, o, n in t[:12]]} for img, t in bad[:10]])


def find_discards(barriers, usage_by_image, names=None):
    """
    A transition from UNDEFINED throws the image's contents away. Right for the first use and for a target that is
    fully overwritten; a bug (black / garbage texture) when the image was written earlier and is read later.
    usage_by_image: {image: [{"event_id", "usage", "kind"}]} from get_resource_usage.
    """
    names = names or {}
    hits, detail = [], []
    for b in barriers:
        for i in b["image_barriers"]:
            if i["old_layout"] != UNDEFINED or i["new_layout"] == UNDEFINED:
                continue
            uses = usage_by_image.get(i["image"], [])
            before = [u for u in uses if u["event_id"] < b["event_id"] and u["kind"] == "write"]
            after = [u for u in uses if u["event_id"] > b["event_id"] and u["kind"] == "read"]
            if before:
                hits.append(b["event_id"])
                detail.append({"event_id": b["event_id"], "image": i["image"], "name": names.get(i["image"], ""),
                               "written_before": [u["event_id"] for u in before[:5]],
                               "read_after": [u["event_id"] for u in after[:5]],
                               "new_layout": i["new"]})
    if not hits:
        return None
    return issue("discards_written_content", "medium", hits,
                 "oldLayout UNDEFINED on an image that was written earlier in the frame: its contents are discarded. If "
                 "the earlier result is still needed (see read_after) this shows up as a black / garbage texture; use "
                 "the real previous layout.", images=detail[:20])


def find_queue_transfers(barriers):
    hits = [b["event_id"] for b in barriers if any(i["queue_transfer"] for i in b["image_barriers"])]
    return issue("queue_ownership_transfer", "info", hits, "Image barriers that transfer queue family ownership.") if hits else None


# ---------------------------------------------------------------- analysis

def analyze(calls, work_events, usage_by_image=None, names=None, other_sync=None, max_barriers=100):
    """
    calls: get_api_calls entries for the barrier calls; work_events: event ids of draws / dispatches / copies / clears
    / pass boundaries; usage_by_image / names: see find_discards. other_sync: {call name: count} of calls not analysed.
    """
    barriers = [b for b in (decode_barrier(c) for c in calls) if b]
    names = names or {}
    issues = [i for i in (
        find_broad_masks(barriers),
        find_discards(barriers, usage_by_image or {}, names),
        find_mergeable(barriers, work_events),
        find_thrash(barriers, names),
        find_queue_transfers(barriers),
    ) if i]
    issues.sort(key=lambda i: (SEVERITY_RANK[i["severity"]], -i["count"]))

    pairs = collections.Counter(("|".join(b["src_stages"]) or "none") + " -> " + ("|".join(b["dst_stages"]) or "none") for b in barriers)
    trans = collections.Counter()
    for b in barriers:
        for i in b["image_barriers"]:
            trans["%s -> %s" % (i["old"], i["new"])] += 1
    per_image = [{"image": img, "name": names.get(img, ""), "layout_changes": len(t)}
                 for img, t in sorted(transitions_by_image(barriers).items(), key=lambda kv: -len(kv[1]))[:20]]

    listing = []
    work = sorted(work_events)
    import bisect
    for b in barriers[:max(0, int(max_barriers))]:
        k = bisect.bisect_left(work, b["event_id"])
        listing.append({
            "event_id": b["event_id"], "src_stages": b["src_stages"], "dst_stages": b["dst_stages"],
            "previous_work_event": work[k - 1] if k > 0 else None, "next_work_event": work[k] if k < len(work) else None,
            "memory_barriers": b["memory_barriers"],
            "buffer_barriers": [{"buffer": x["buffer"], "src_access": x["src_access"], "dst_access": x["dst_access"]} for x in b["buffer_barriers"]],
            "image_barriers": [{"image": x["image"], "name": names.get(x["image"], ""), "layout": "%s -> %s" % (x["old"], x["new"]),
                                "src_access": x["src_access"], "dst_access": x["dst_access"], "aspects": x["aspects"]}
                               for x in b["image_barriers"]],
        })
    return {
        "barrier_count": len(barriers),
        "image_barriers": sum(len(b["image_barriers"]) for b in barriers),
        "buffer_barriers": sum(len(b["buffer_barriers"]) for b in barriers),
        "layout_transitions": dict(trans.most_common()),
        "stage_pairs": dict(pairs.most_common(15)),
        "images_with_layout_changes": per_image,
        "issue_count": len(issues),
        "issues": issues,
        "not_analysed": dict(other_sync or {}),
        "barriers": listing,
        "barriers_truncated": len(barriers) > len(listing),
        "scope": "legacy vkCmdPipelineBarrier only; layouts are not simulated through render passes and stage / access "
                 "legality is not checked (get_debug_messages shows the validation layer's findings)",
    }
