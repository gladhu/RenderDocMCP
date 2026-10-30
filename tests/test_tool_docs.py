"""Tool descriptions are what the model reads to pick a tool: a reference to a tool that does not exist misleads it."""

import asyncio
import re
import unittest

from fastmcp import Client

from mcp_server.server import mcp

TOOL_LIKE = re.compile(r"\b(?:get|find|list|analyze|estimate|read|save|pick|pixel|render|replace|revert|debug|diff|"
                       r"compare|snapshot|stat|open|export|frame|diagnose|sample)_[a-z0-9_]+\b")
# identifiers that look like tool names but are issue types / result fields, not tools
NOT_TOOLS = {"render_target_reopened", "render_target", "render_state"}


def list_tools():
    async def run():
        async with Client(mcp) as c:
            return await c.list_tools()
    return asyncio.run(run())


class ToolDescriptions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = list_tools()

    def test_every_tool_has_a_description(self):
        for t in self.tools:
            self.assertTrue((t.description or "").strip(), "%s has no description" % t.name)

    def test_references_to_other_tools_exist(self):
        names = {t.name for t in self.tools}
        params = {p for t in self.tools for p in (t.inputSchema or {}).get("properties", {})}
        for t in self.tools:
            for ref in sorted(set(TOOL_LIKE.findall(t.description or ""))):
                if ref in names or ref in params or ref in NOT_TOOLS:
                    continue
                self.fail("%s's description mentions %r, which is not a tool (a wrong tool name, or add it to "
                          "NOT_TOOLS if it is not meant as one)" % (t.name, ref))

    def test_the_overlapping_tools_point_at_each_other(self):
        d = dict((t.name, t.description) for t in self.tools)
        for a, b in (("get_texture_info", "get_texture_detail"), ("get_shader_usage", "list_shaders"),
                     ("list_shaders", "get_shader_usage"), ("sample_pixel_region", "find_pixels"),
                     ("read_texture_stats", "find_pixels"), ("get_shader_info", "get_shader_source"),
                     ("get_resource_usage", "find_draws_by_resource")):
            self.assertIn(b, d[a], "%s should say when to use %s instead" % (a, b))

    def test_total_description_size_stays_bounded(self):
        """every session pays for these in context; growing past the budget should be a decision, not an accident"""
        total = sum(len(t.description or "") for t in self.tools)
        self.assertLess(total, 42000, "tool descriptions total %d chars: trim, or raise this budget on purpose" % total)


if __name__ == "__main__":
    unittest.main()
