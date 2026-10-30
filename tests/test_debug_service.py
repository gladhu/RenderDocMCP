"""debug_service: filtering, grouping and capping of API debug messages."""

import types
import unittest

from tests._stubs import load_service

dbg = load_service("debug_service")


def _raw(eid, sev, msg_id, text, category=14, source=0):
    return types.SimpleNamespace(eventId=eid, severity=sev, messageID=msg_id, description=text,
                                 category=category, source=source)


def _m(eid, sev, msg_id, text, category="Execution", source="API"):
    return {"event_id": eid, "severity": sev, "category": category, "source": source,
            "message_id": msg_id, "description": text}


class Normalise(unittest.TestCase):
    def test_enum_names(self):
        m = dbg.normalise(_raw(7, 0, 12, "bad", category=14, source=2))
        self.assertEqual((m["severity"], m["category"], m["source"]), ("high", "Performance", "IncorrectAPIUse"))
        self.assertEqual((m["event_id"], m["message_id"], m["description"]), (7, 12, "bad"))

    def test_unknown_enum_values_keep_their_number(self):
        m = dbg.normalise(_raw(1, 9, 1, "x", category=99, source=99))
        self.assertEqual((m["severity"], m["category"], m["source"]), ("9", "99", "99"))


class Summarise(unittest.TestCase):
    def setUp(self):
        self.msgs = [
            _m(10, "info", 1, "a"), _m(11, "info", 1, "a"), _m(12, "info", 1, "a"),
            _m(20, "medium", 2, "layout wrong", "State_Setting", "IncorrectAPIUse"),
            _m(30, "high", 3, "descriptor invalid", "Execution", "IncorrectAPIUse"),
            _m(31, "high", 3, "descriptor invalid", "Execution", "IncorrectAPIUse"),
        ]

    def test_groups_are_counted_and_ranked_by_severity_then_count(self):
        r = dbg.summarise(self.msgs)
        self.assertEqual(r["total_matched"], 6)
        self.assertEqual([g["message_id"] for g in r["groups"]], [3, 2, 1])
        self.assertEqual([g["count"] for g in r["groups"]], [2, 1, 3])
        self.assertEqual(r["groups"][2]["sample_event_ids"], [10, 11, 12])
        self.assertEqual((r["groups"][0]["first_event_id"], r["groups"][0]["last_event_id"]), (30, 31))
        self.assertEqual(r["by_severity"], {"info": 3, "medium": 1, "high": 2})

    def test_same_id_different_text_are_different_groups(self):
        r = dbg.summarise([_m(1, "low", 5, "x"), _m(2, "low", 5, "y")])
        self.assertEqual(r["group_count"], 2)

    def test_min_severity(self):
        r = dbg.summarise(self.msgs, min_severity="medium")
        self.assertEqual(r["total_matched"], 3)
        self.assertEqual(set(r["by_severity"]), {"medium", "high"})
        self.assertEqual(dbg.summarise(self.msgs, min_severity="high")["total_matched"], 2)

    def test_bad_severity_raises(self):
        with self.assertRaises(ValueError):
            dbg.summarise(self.msgs, min_severity="urgent")

    def test_substring_filters_ignore_case(self):
        self.assertEqual(dbg.summarise(self.msgs, category="state_")["total_matched"], 1)
        self.assertEqual(dbg.summarise(self.msgs, source="incorrect")["total_matched"], 3)
        self.assertEqual(dbg.summarise(self.msgs, text_filter="DESCRIPTOR")["total_matched"], 2)

    def test_event_range_is_inclusive(self):
        r = dbg.summarise(self.msgs, event_id_min=11, event_id_max=30)
        self.assertEqual(sorted(m["event_id"] for m in r["messages"]), [11, 12, 20, 30])

    def test_caps_report_truncation_but_counts_cover_everything(self):
        many = [_m(i, "info", i % 7, "t%d" % (i % 7)) for i in range(200)]
        r = dbg.summarise(many, max_messages=10, max_groups=3)
        self.assertEqual(r["total_matched"], 200)
        self.assertEqual(sum(r["by_severity"].values()), 200)
        self.assertEqual((len(r["messages"]), r["truncated"]), (10, True))
        self.assertEqual((len(r["groups"]), r["group_count"], r["groups_truncated"]), (3, 7, True))

    def test_empty(self):
        r = dbg.summarise([])
        self.assertEqual((r["total_matched"], r["groups"], r["messages"], r["truncated"]), (0, [], [], False))


class ServiceWiring(unittest.TestCase):
    def test_reads_ui_side_list_and_reports_capture_total(self):
        ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            DebugMessages=lambda: [_raw(1, 1, 4, "m"), _raw(2, 3, 5, "n")])
        r = dbg.DebugService(ctx, None).get_debug_messages(min_severity="medium")
        self.assertEqual((r["total_in_capture"], r["total_matched"]), (2, 1))

    def test_requires_a_capture(self):
        ctx = types.SimpleNamespace(IsCaptureLoaded=lambda: False)
        with self.assertRaises(ValueError):
            dbg.DebugService(ctx, None).get_debug_messages()


if __name__ == "__main__":
    unittest.main()
