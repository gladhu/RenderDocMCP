"""vk_passes: Vulkan render pass model, load/store ops per attachment, dynamic rendering."""

import unittest

from tests._stubs import load_service

vk = load_service("vk_passes")
bw = load_service("bandwidth_service")

VK_FORMAT_R8G8B8A8_SRGB = 43
VK_FORMAT_D24_UNORM_S8_UINT = 129
LOAD, CLEAR, DONT_CARE = 0, 1, 2          # VkAttachmentLoadOp
STORE, STORE_DONT_CARE = 0, 1             # VkAttachmentStoreOp


def _ref(i):
    return {"attachment": i, "layout": 2}


def _att(fmt, samples, load_op, store_op, stencil_load=DONT_CARE, stencil_store=STORE_DONT_CARE):
    return {"format": fmt, "samples": samples, "loadOp": load_op, "storeOp": store_op,
            "stencilLoadOp": stencil_load, "stencilStoreOp": stencil_store}


def _tex(name, fmt, bpp, samples=1, swapchain=False):
    return dict(name=name, width=100, height=100, format=fmt, samples=samples, bpp=bpp, swapchain=swapchain)


def _ev(eid, name, params=None, action=None):
    return dict(eid=eid, name=name, params=params or {}, marker="M", action=action)


def _draw(eid):
    return _ev(eid, "vkCmdDraw", action={"flags": 2, "outputs": [], "depth": None})


def _begin(eid, rp, fb):
    return _ev(eid, "vkCmdBeginRenderPass", {"RenderPassBegin": {
        "renderPass": rp, "framebuffer": fb, "renderArea": {"extent": {"width": 100, "height": 100}}}})


CHUNKS = [
    # RP1: 4x MSAA color cleared + not stored, resolved into a 1x target, 4x depth loaded + not stored
    ("vkCreateRenderPass2", {"RenderPass": "RP1", "CreateInfo": {
        "pAttachments": [_att(VK_FORMAT_R8G8B8A8_SRGB, 4, CLEAR, STORE_DONT_CARE),
                         _att(VK_FORMAT_R8G8B8A8_SRGB, 1, DONT_CARE, STORE),
                         _att(VK_FORMAT_D24_UNORM_S8_UINT, 4, LOAD, STORE_DONT_CARE, DONT_CARE, STORE_DONT_CARE)],
        "pSubpasses": [{"pColorAttachments": [_ref(0)], "pResolveAttachments": [_ref(1)],
                        "pDepthStencilAttachment": _ref(2)}]}}),
    # RP2: color LOAD/STORE; depth CLEAR/DONT_CARE but stencil DONT_CARE/STORE
    ("vkCreateRenderPass", {"RenderPass": "RP2", "CreateInfo": {
        "pAttachments": [_att(VK_FORMAT_R8G8B8A8_SRGB, 1, LOAD, STORE),
                         _att(VK_FORMAT_D24_UNORM_S8_UINT, 1, CLEAR, STORE_DONT_CARE, DONT_CARE, STORE)],
        "pSubpasses": [{"pColorAttachments": [_ref(0)], "pResolveAttachments": [],
                        "pDepthStencilAttachment": _ref(1)}]}}),
    ("vkCreateFramebuffer", {"Framebuffer": "FB1", "CreateInfo": {
        "renderPass": "RP1", "pAttachments": ["V1", "V2", "V3"], "width": 100, "height": 100}}),
    ("vkCreateFramebuffer", {"Framebuffer": "FB2", "CreateInfo": {
        "renderPass": "RP2", "pAttachments": ["V1b", "V3b"], "width": 100, "height": 100}}),
    ("vkCreateImageView", {"View": "V1", "CreateInfo": {"image": "IMG_MS", "format": VK_FORMAT_R8G8B8A8_SRGB}}),
    ("vkCreateImageView", {"View": "V2", "CreateInfo": {"image": "IMG_RES", "format": VK_FORMAT_R8G8B8A8_SRGB}}),
    ("vkCreateImageView", {"View": "V3", "CreateInfo": {"image": "IMG_DMS", "format": VK_FORMAT_D24_UNORM_S8_UINT}}),
    ("vkCreateImageView", {"View": "V1b", "CreateInfo": {"image": "IMG_RES", "format": VK_FORMAT_R8G8B8A8_SRGB}}),
    ("vkCreateImageView", {"View": "V3b", "CreateInfo": {"image": "IMG_D", "format": VK_FORMAT_D24_UNORM_S8_UINT}}),
]

TEXTURES = {
    "IMG_MS": _tex("MSColor", "R8G8B8A8_SRGB", 4, 4),
    "IMG_RES": _tex("Color", "R8G8B8A8_SRGB", 4),
    "IMG_DMS": _tex("MSDepth", "D24S8", 4, 4),
    "IMG_D": _tex("Depth", "D24S8", 4),
}

STREAM = [_begin(10, "RP1", "FB1"), _draw(11), _draw(12), _ev(13, "vkCmdEndRenderPass"),
          _begin(20, "RP2", "FB2"), _draw(21), _ev(22, "vkCmdEndRenderPass"),
          _begin(30, "NOPE", "NOFB"), _ev(31, "vkCmdEndRenderPass")]


class VulkanModelTest(unittest.TestCase):
    def test_render_pass_attachment_indices(self):
        rp = vk.build_model(CHUNKS)["renderpasses"]["RP1"]
        self.assertEqual(rp["colors"], [0])
        self.assertEqual(rp["resolves"], [1])
        self.assertEqual(rp["depth"], 2)

    def test_unused_attachment_reference_is_none(self):
        self.assertIsNone(vk._ref_index({"attachment": 4294967295}))   # VK_ATTACHMENT_UNUSED
        self.assertIsNone(vk._ref_index(None))


class VulkanPassesTest(unittest.TestCase):
    def setUp(self):
        self.model = vk.build_model(CHUNKS)
        self.passes, self.problems = vk.build_vulkan_passes(STREAM, self.model, TEXTURES)

    def _attachments(self, index):
        return {a["role"]: a for a in self.passes[index]["attachments"]}

    def test_unknown_render_pass_reported_as_problem(self):
        self.assertEqual(len(self.passes), 2)
        self.assertEqual(len(self.problems), 1)
        self.assertEqual(self.problems[0]["event_id"], 30)

    def test_msaa_pass_roles(self):
        self.assertEqual(set(self._attachments(0)), {"color0", "resolve0", "depth"})

    def test_cleared_msaa_color_not_loaded_nor_stored(self):
        color = self._attachments(0)["color0"]
        self.assertFalse(color["load"]["needed"])
        self.assertEqual(color["load"]["skipped_by"], "clear")
        self.assertFalse(color["store"]["needed"])       # MSAA samples stay in tile memory: good
        self.assertEqual(color["samples"], 4)

    def test_resolve_target_stored_at_single_sample_size(self):
        resolve = self._attachments(0)["resolve0"]
        self.assertTrue(resolve["store"]["needed"])
        self.assertEqual(resolve["bytes"], 100 * 100 * 4)

    def test_msaa_depth_load_counts_all_samples(self):
        depth = self._attachments(0)["depth"]
        self.assertTrue(depth["load"]["needed"])
        self.assertEqual(depth["bytes"], 100 * 100 * 4 * 4)

    def test_pass_draw_count_and_end_event(self):
        self.assertEqual(self.passes[0]["draws"], 2)
        self.assertEqual(self.passes[0]["end"], 13)

    def test_stencil_store_op_keeps_depth_store(self):
        a = self._attachments(1)
        self.assertTrue(a["color0"]["load"]["needed"])
        self.assertTrue(a["color0"]["store"]["needed"])
        self.assertFalse(a["depth"]["load"]["needed"])
        self.assertTrue(a["depth"]["store"]["needed"])

    def test_evaluation_and_issues(self):
        bw.evaluate_passes(self.passes, TEXTURES, {"IMG_RES": [99]})
        color = self._attachments(1)["color0"]
        self.assertTrue(color["load"]["source"].startswith("pass 0"))
        self.assertIs(color["load"]["reopened"], False)       # written by the directly preceding pass
        issues = bw.find_issues(self.passes, [], api="vulkan")
        wasted = [i for i in issues if i["type"] == "wasted_store"]
        self.assertTrue(wasted, issues)
        self.assertTrue(any("storeOp" in i["suggestion"] for i in wasted))
        self.assertEqual(self.passes[0]["load_bytes"], 100 * 100 * 4 * 4)
        self.assertEqual(self.passes[0]["store_bytes"], 100 * 100 * 4)


class DynamicRenderingTest(unittest.TestCase):
    def test_rendering_info_attachments(self):
        model = vk.build_model(CHUNKS)
        info = {"renderArea": {"extent": {"width": 100, "height": 100}},
                "pColorAttachments": [{"imageView": "V1b", "loadOp": LOAD, "storeOp": STORE, "resolveImageView": "V2"}],
                "pDepthAttachment": {"imageView": "V3b", "loadOp": CLEAR, "storeOp": STORE_DONT_CARE}}
        atts = vk._rendering_attachments(info, model, TEXTURES)
        self.assertEqual([a["role"] for a in atts], ["color0", "color0_resolve", "depth"])
        self.assertTrue(atts[0]["load"]["needed"])
        self.assertFalse(atts[2]["load"]["needed"])
        self.assertFalse(atts[2]["store"]["needed"])


if __name__ == "__main__":
    unittest.main()
