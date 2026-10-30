"""vk_sync: barrier decoding and the rules, using barriers recorded from a real Mali-G610 Vulkan capture."""

import unittest

from mcp_server import vk_sync as vs


def pcall(eid, src, dst, buffers=(), images=(), memory=0, cb="ResourceId::368", name="vkCmdPipelineBarrier"):
    return {"event_id": eid, "name": name, "params": [
        {"name": "commandBuffer", "value": cb}, {"name": "srcStageMask", "value": src}, {"name": "destStageMask", "value": dst},
        {"name": "dependencyFlags", "value": 0}, {"name": "memoryBarrierCount", "value": memory},
        {"name": "pMemoryBarriers", "value": [{}] * memory},
        {"name": "pBufferMemoryBarriers", "value": [
            {"srcAccessMask": a, "dstAccessMask": d, "srcQueueFamilyIndex": -1, "dstQueueFamilyIndex": -1, "buffer": b}
            for b, a, d in buffers]},
        {"name": "pImageMemoryBarriers", "value": [
            {"srcAccessMask": sa, "dstAccessMask": da, "oldLayout": o, "newLayout": n, "image": img,
             "srcQueueFamilyIndex": qs, "dstQueueFamilyIndex": qd,
             "subresourceRange": {"aspectMask": asp, "baseMipLevel": 0, "levelCount": 1, "baseArrayLayer": 0, "layerCount": 1}}
            for img, o, n, sa, da, asp, qs, qd in images]}]}


def img(image, old, new, sa=0, da=0, asp=1, qs=-1, qd=-1):
    return (image, old, new, sa, da, asp, qs, qd)


# the 15 barriers of test-Vulkan_02 (Mali-G610): (event, src stages, dst stages, buffers, images)
REAL = [
    pcall(111, 128, 1024, images=[img("ResourceId::1699", 5, 2, 32, 384)]),
    pcall(112, 768, 768, images=[img("ResourceId::1703", 3, 3, 1024, 1536, 6)]),
    pcall(359, 1024, 128, images=[img("ResourceId::1699", 2, 5, 256, 32)]),
    pcall(360, 8192, 1024, images=[img("ResourceId::374", 0, 2, 32768, 256)]),
    pcall(361, 1024, 1024, images=[img("ResourceId::374", 2, 2, 256, 384)]),
    pcall(362, 768, 768, images=[img("ResourceId::399", 3, 3, 1024, 1536, 6)]),
    pcall(442, 1024, 8192, images=[img("ResourceId::374", 2, 1000001002, 256, 32768)]),
    pcall(30, 2052, 2048, buffers=[("ResourceId::1687", 68, 96)]),
]
WORK = [31, 110, 113, 358, 363, 373, 441]


class Decode(unittest.TestCase):
    def test_flag_names(self):
        self.assertEqual(vs.decode_flags(2052, vs.STAGES), ["VERTEX_INPUT", "COMPUTE_SHADER"])
        self.assertEqual(vs.decode_flags(768, vs.STAGES), ["EARLY_FRAGMENT_TESTS", "LATE_FRAGMENT_TESTS"])
        self.assertEqual(vs.decode_flags(0, vs.STAGES), [])
        self.assertEqual(vs.decode_flags(1536, vs.ACCESS), ["DEPTH_STENCIL_ATTACHMENT_READ", "DEPTH_STENCIL_ATTACHMENT_WRITE"])
        self.assertEqual(vs.decode_flags(0x1000000, vs.STAGES), ["0x1000000"])          # unknown bit kept, not dropped

    def test_real_present_transition(self):
        b = vs.decode_barrier(REAL[6])
        i = b["image_barriers"][0]
        self.assertEqual((i["old"], i["new"]), ("COLOR_ATTACHMENT_OPTIMAL", "PRESENT_SRC_KHR"))
        self.assertEqual((b["src_stages"], b["dst_stages"]), (["COLOR_ATTACHMENT_OUTPUT"], ["BOTTOM_OF_PIPE"]))
        self.assertEqual(i["src_access"], ["COLOR_ATTACHMENT_WRITE"])

    def test_buffer_barrier_and_aspects(self):
        b = vs.decode_barrier(REAL[7])
        self.assertEqual(b["buffer_barriers"][0]["buffer"], "ResourceId::1687")
        self.assertEqual(vs.decode_barrier(REAL[1])["image_barriers"][0]["aspects"], ["depth", "stencil"])

    def test_other_calls_are_ignored_and_unknown_layouts_named(self):
        self.assertIsNone(vs.decode_barrier({"event_id": 1, "name": "vkCmdPipelineBarrier2", "params": []}))
        self.assertEqual(vs.layout_name(1000117000), "LAYOUT_1000117000")

    def test_queue_transfer_needs_two_real_families(self):
        c = pcall(5, 1, 1, images=[img("I", 2, 2, qs=0, qd=1), img("J", 2, 2, qs=-1, qd=-1), img("K", 2, 2, qs=0, qd=0)])
        self.assertEqual([i["queue_transfer"] for i in vs.decode_barrier(c)["image_barriers"]], [True, False, False])


class RealCapture(unittest.TestCase):
    def setUp(self):
        self.r = vs.analyze(REAL, WORK, usage_by_image={}, names={"ResourceId::374": "Swapchain Image 374"})

    def test_counts_and_transitions(self):
        self.assertEqual((self.r["barrier_count"], self.r["image_barriers"], self.r["buffer_barriers"]), (8, 7, 1))
        self.assertEqual(self.r["layout_transitions"]["COLOR_ATTACHMENT_OPTIMAL -> PRESENT_SRC_KHR"], 1)
        self.assertEqual(self.r["layout_transitions"]["DEPTH_STENCIL_ATTACHMENT_OPTIMAL -> DEPTH_STENCIL_ATTACHMENT_OPTIMAL"], 2)

    def test_nothing_is_flagged_that_is_not_there(self):
        kinds = [i["type"] for i in self.r["issues"]]
        self.assertNotIn("broad_stage_mask", kinds)
        self.assertNotIn("discards_written_content", kinds)         # 374: UNDEFINED -> COLOR on first use
        self.assertNotIn("layout_thrash", kinds)

    def test_back_to_back_barriers_are_found_as_mergeable(self):
        m = [i for i in self.r["issues"] if i["type"] == "mergeable_barriers"][0]
        # work events 110 / 113 are around 111-112 (not between them), 358 and 363 are around 359-362
        self.assertEqual(m["groups"], [[111, 112], [359, 360, 361, 362]])
        self.assertEqual(m["barriers_saved"], 1 + 3)

    def test_listing_points_at_neighbouring_work(self):
        by = dict((b["event_id"], b) for b in self.r["barriers"])
        self.assertEqual((by[359]["previous_work_event"], by[359]["next_work_event"]), (358, 363))
        self.assertEqual(by[360]["image_barriers"][0]["name"], "Swapchain Image 374")

    def test_listing_is_capped(self):
        r = vs.analyze(REAL, WORK, max_barriers=3)
        self.assertEqual((len(r["barriers"]), r["barriers_truncated"], r["barrier_count"]), (3, True, 8))


class Rules(unittest.TestCase):
    def test_all_commands_and_all_graphics_are_flagged(self):
        r = vs.analyze([pcall(1, 0x10000, 0x10000), pcall(2, 0x8000, 0x80), pcall(3, 0x80, 0x400)], [])
        i = [x for x in r["issues"] if x["type"] == "broad_stage_mask"][0]
        self.assertEqual((i["event_ids"], i["severity"]), ([1, 2], "medium"))

    def test_different_command_buffers_are_not_merged(self):
        r = vs.analyze([pcall(1, 1, 1, cb="A"), pcall(2, 1, 1, cb="B")], [])
        self.assertNotIn("mergeable_barriers", [i["type"] for i in r["issues"]])

    def test_work_between_prevents_merging(self):
        self.assertIsNone(vs.find_mergeable([vs.decode_barrier(pcall(1, 1, 1)), vs.decode_barrier(pcall(9, 1, 1))], [5]))
        self.assertIsNotNone(vs.find_mergeable([vs.decode_barrier(pcall(1, 1, 1)), vs.decode_barrier(pcall(9, 1, 1))], [20]))

    def test_layout_thrash_needs_real_changes_not_same_layout_barriers(self):
        flips = [pcall(10 + n, 1, 1, images=[img("T", 2 if n % 2 else 5, 5 if n % 2 else 2)]) for n in range(5)]
        r = vs.analyze(flips, [], names={"T": "Tex"})
        t = [i for i in r["issues"] if i["type"] == "layout_thrash"][0]
        self.assertEqual((t["images"][0]["name"], t["images"][0]["transitions"]), ("Tex", 5))
        same = [pcall(10 + n, 1, 1, images=[img("T", 3, 3)]) for n in range(6)]
        self.assertNotIn("layout_thrash", [i["type"] for i in vs.analyze(same, [])["issues"]])

    def test_undefined_after_a_write_is_flagged_with_evidence(self):
        usage = {"T": [{"event_id": 5, "usage": "ColorTarget", "kind": "write"}, {"event_id": 30, "usage": "PS_Resource", "kind": "read"}]}
        r = vs.analyze([pcall(10, 1, 1, images=[img("T", 0, 5)])], [], usage_by_image=usage)
        d = [i for i in r["issues"] if i["type"] == "discards_written_content"][0]
        self.assertEqual((d["severity"], d["images"][0]["written_before"], d["images"][0]["read_after"]), ("medium", [5], [30]))

    def test_undefined_on_first_use_or_after_only_barrier_usage_is_fine(self):
        usage = {"T": [{"event_id": 5, "usage": "Barrier", "kind": "other"}, {"event_id": 6, "usage": "Discard", "kind": "other"},
                       {"event_id": 30, "usage": "ColorTarget", "kind": "write"}]}
        r = vs.analyze([pcall(10, 1, 1, images=[img("T", 0, 2)])], [], usage_by_image=usage)
        self.assertNotIn("discards_written_content", [i["type"] for i in r["issues"]])
        # a transition TO undefined is not a discard of anything we care about
        self.assertIsNone(vs.find_discards([vs.decode_barrier(pcall(10, 1, 1, images=[img("T", 2, 0)]))], usage))

    def test_issues_are_sorted_by_severity(self):
        usage = {"T": [{"event_id": 1, "usage": "ColorTarget", "kind": "write"}]}
        r = vs.analyze([pcall(10, 0x10000, 1, images=[img("T", 0, 5)]), pcall(11, 1, 1)], [], usage_by_image=usage)
        self.assertEqual([i["severity"] for i in r["issues"]], sorted([i["severity"] for i in r["issues"]], key=vs.SEVERITY_RANK.get))

    def test_not_analysed_calls_are_reported_and_empty_input_is_fine(self):
        r = vs.analyze([], [], other_sync={"vkCmdPipelineBarrier2": 4})
        self.assertEqual((r["barrier_count"], r["issues"], r["not_analysed"]), (0, [], {"vkCmdPipelineBarrier2": 4}))


if __name__ == "__main__":
    unittest.main()
