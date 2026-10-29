"""result_cache: envelopes for oversized tool results, paging / searching cached results, the middleware."""

import asyncio
import json
import types
import unittest
from unittest import mock

from tests import _stubs  # noqa: F401  (puts the repo root on sys.path)
from mcp_server import result_cache as rc

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

LIMIT = 20000


def _big_payload():
    return {"total": 5000,
            "actions": [{"event_id": i, "name": "glDrawElements(%d)" % i, "flags": ["Drawcall"]} for i in range(1, 2001)],
            "markers": ["m%d" % i for i in range(30)]}


class _CacheTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(rc, "MAX_DIRECT_CHARS", LIMIT)
        patcher.start()
        self.addCleanup(patcher.stop)
        rc.clear()
        self.addCleanup(rc.clear)


class EnvelopeTest(_CacheTestCase):
    def test_small_result_not_cached(self):
        self.assertIsNone(rc.store_if_large("get_frame_summary", {"loaded": True, "api": "OpenGL"}))

    def test_large_result_replaced_by_envelope(self):
        env = rc.store_if_large("get_frame_summary", _big_payload())
        self.assertEqual(env["status"], "cached")
        self.assertTrue(env["message_id"].startswith("msg_"))
        self.assertEqual(env["available_views"], [{"key": "actions", "type": "list", "count": 2000},
                                                  {"key": "markers", "type": "list", "count": 30}])
        self.assertEqual(list(env["preview"]), ["total", "actions", "markers"])
        self.assertEqual(env["preview"]["actions"]["count"], 2000)
        self.assertEqual(len(env["preview"]["actions"]["first"]), rc.PREVIEW_ITEMS)
        self.assertEqual(env["next_step"]["arguments"]["page_size"], rc.DEFAULT_PAGE_SIZE)
        self.assertLess(len(json.dumps(env)), LIMIT)


class ListPagingTest(_CacheTestCase):
    def setUp(self):
        super().setUp()
        self.mid = rc.store_if_large("get_frame_summary", _big_payload())["message_id"]

    def test_default_collection_is_largest_list(self):
        p = rc.query_cached_message(self.mid, page=2)
        self.assertEqual(p["collection"], "actions")
        self.assertEqual((p["page"], p["total_pages"], p["total_items"]), (2, 40, 2000))
        self.assertEqual(len(p["items"]), 50)
        self.assertEqual(p["items"][0]["event_id"], 51)
        self.assertEqual(p["next_page"], 3)

    def test_last_page_has_no_next_page(self):
        p = rc.query_cached_message(self.mid, page=40)
        self.assertEqual(p["items"][-1]["event_id"], 2000)
        self.assertNotIn("next_page", p)

    def test_page_size_clamped(self):
        p = rc.query_cached_message(self.mid, page_size=10000)
        self.assertEqual(p["page_size"], rc.MAX_PAGE_SIZE)
        self.assertEqual(rc.query_cached_message(self.mid, page_size=0)["page_size"], 1)

    def test_search_filters_items(self):
        s = rc.query_cached_message(self.mid, search="(1999)")
        self.assertEqual(s["search"], {"query": "(1999)", "matches": 1})
        self.assertEqual([i["event_id"] for i in s["items"]], [1999])

    def test_named_collection_and_other_fields(self):
        m = rc.query_cached_message(self.mid, collection="markers", page_size=10, page=3)
        self.assertEqual(m["items"], ["m%d" % i for i in range(20, 30)])
        self.assertEqual(m["other_fields"], {"total": 5000})

    def test_unknown_message_raises(self):
        with self.assertRaises(ValueError):
            rc.query_cached_message("msg_nope")

    def test_unknown_collection_raises(self):
        with self.assertRaisesRegex(ValueError, "actions"):
            rc.query_cached_message(self.mid, collection="zzz")


class LinePagingTest(_CacheTestCase):
    def test_payload_without_lists_paged_by_lines(self):
        payload = {"shaders": {"s%d" % i: {"cb": list(range(20))} for i in range(400)}}
        env = rc.store_if_large("get_frame_summary", payload)
        self.assertEqual([(v["key"], v["type"]) for v in env["available_views"]], [("shaders", "lines")])
        p = rc.query_cached_message(env["message_id"])
        self.assertEqual(p["collection"], "(lines)")
        self.assertEqual(len(p["lines"]), rc.DEFAULT_PAGE_SIZE * 4)
        s = rc.query_cached_message(env["message_id"], search="s399")
        self.assertEqual(s["search"]["matches"], 1)
        self.assertEqual(len(s["lines"]), 1)

    def test_dict_field_paged_by_collection_name(self):
        payload = {"shaders": {"s%d" % i: {"cb": list(range(20))} for i in range(400)}}
        mid = rc.store_if_large("get_frame_summary", payload)["message_id"]
        p = rc.query_cached_message(mid, collection="shaders", search="\"s7\"")
        self.assertEqual(p["collection"], "shaders")
        self.assertEqual(p["search"]["matches"], 1)


class LargeStringFieldTest(_CacheTestCase):
    """A big non-list field (shader source) is a "lines" view next to the list views."""

    def setUp(self):
        super().setUp()
        self.source = "\n".join("uniform vec4 _Color%d;" % i for i in range(3000))
        self.payload = {"event_id": 42, "stage": "pixel", "source": self.source,
                        "bindings": [{"slot": i, "name": "tex%d" % i} for i in range(5)]}
        self.env = rc.store_if_large("get_shader_info", self.payload)
        self.mid = self.env["message_id"]

    def test_views_include_lines_view_for_source(self):
        views = {v["key"]: v for v in self.env["available_views"]}
        self.assertEqual(views["bindings"], {"key": "bindings", "type": "list", "count": 5})
        self.assertEqual(views["source"]["type"], "lines")
        self.assertEqual(views["source"]["chars"], len(json.dumps(self.source)))
        self.assertTrue(self.env["preview"]["source"].startswith("<str, "))

    def test_default_query_pages_list_and_points_at_source(self):
        p = rc.query_cached_message(self.mid)
        self.assertEqual(p["collection"], "bindings")
        self.assertEqual(p["other_fields"]["event_id"], 42)
        self.assertEqual(p["other_fields"]["stage"], "pixel")
        self.assertEqual(p["other_fields"]["source"],
                         "<%d chars; page it with collection='source'>" % len(json.dumps(self.source)))

    def test_source_paged_line_by_line(self):
        p = rc.query_cached_message(self.mid, collection="source", page=2, page_size=50)
        self.assertEqual(p["collection"], "source")
        self.assertEqual(p["total_lines"], 3000)
        self.assertEqual(p["total_pages"], 15)                      # 200 lines per page
        self.assertEqual(p["lines"][0], "uniform vec4 _Color200;")  # raw text, not JSON-escaped
        self.assertEqual(len(p["lines"]), 200)
        self.assertEqual(p["next_page"], 3)
        self.assertNotIn("other_fields", p)

    def test_source_search(self):
        p = rc.query_cached_message(self.mid, collection="source", search="_color2999;")
        self.assertEqual(p["search"], {"query": "_color2999;", "matches": 1})
        self.assertEqual(p["lines"], ["uniform vec4 _Color2999;"])

    def test_small_scalar_field_is_not_a_collection(self):
        with self.assertRaisesRegex(ValueError, "source"):
            rc.query_cached_message(self.mid, collection="stage")


class MessageCacheTest(unittest.TestCase):
    def test_lru_eviction(self):
        cache = rc.MessageCache(max_entries=2)
        a = cache.put("t", 1, 1)["message_id"]
        b = cache.put("t", 2, 1)["message_id"]
        cache.get(a)                                   # a becomes most recent
        c = cache.put("t", 3, 1)["message_id"]
        self.assertIsNone(cache.get(b))
        self.assertEqual(cache.get(a)["payload"], 1)
        self.assertEqual(cache.get(c)["payload"], 3)


class MiddlewareTest(_CacheTestCase):
    def _run(self, tool_name, result):
        async def call_next(_context):
            return result
        context = types.SimpleNamespace(message=types.SimpleNamespace(name=tool_name))
        return asyncio.run(rc.ResultCacheMiddleware().on_call_tool(context, call_next))

    def test_large_structured_result_replaced(self):
        payload = _big_payload()
        out = self._run("get_draw_calls", ToolResult(content=[TextContent(type="text", text=json.dumps(payload))],
                                                     structured_content=payload))
        self.assertEqual(out.structured_content["status"], "cached")
        self.assertEqual(json.loads(out.content[0].text)["message_id"], out.structured_content["message_id"])

    def test_small_result_passed_through(self):
        original = ToolResult(content=[TextContent(type="text", text="{}")], structured_content={"ok": True})
        self.assertIs(self._run("get_frame_summary", original), original)

    def test_exempt_tool_passed_through(self):
        original = ToolResult(content=[TextContent(type="text", text="A" * 60000)],
                              structured_content={"data": "A" * 60000})
        self.assertIs(self._run("get_texture_data", original), original)

    def test_wrapped_list_result_unwrapped(self):
        items = [{"i": i, "pad": "x" * 50} for i in range(1000)]
        out = self._run("list_buffers", ToolResult(content=[TextContent(type="text", text=json.dumps(items))],
                                                   structured_content={"result": items}))
        self.assertEqual(out.structured_content["available_views"], [{"key": "(root)", "type": "list", "count": 1000}])

    def test_text_only_result_parsed(self):
        payload = _big_payload()
        out = self._run("get_draw_calls", ToolResult(content=[TextContent(type="text", text=json.dumps(payload))]))
        self.assertEqual(out.structured_content["status"], "cached")


if __name__ == "__main__":
    unittest.main()
