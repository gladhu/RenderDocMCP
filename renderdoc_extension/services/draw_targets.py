"""
Render targets of draw calls on APIs whose actions do not carry them.

D3D / GL fill ActionDescription.outputs / depthOut; Vulkan leaves them empty (the targets belong to the
render pass). event_targets() derives {draw event id: ([colour ids], depth id)} once per request from the
render pass structure, without replaying any draw.
"""

from .bandwidth_service import texture_table
from . import vk_passes


def vulkan_passes(ctx, controller, api):
    """Tile passes of a Vulkan capture (see vk_passes); [] for other APIs"""
    if api != "Vulkan":
        return []
    textures, _ = texture_table(controller, ctx.GetResourceName)
    return vk_passes.collect_passes(controller, textures)[0]


def event_targets(ctx, controller, api, passes=None):
    """{event id: ([colour image ids], depth image id or None)}; empty when the API fills action.outputs itself"""
    if api != "Vulkan":
        return {}
    return vk_passes.targets_by_event(passes if passes is not None else vulkan_passes(ctx, controller, api))
