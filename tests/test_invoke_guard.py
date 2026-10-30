"""guarded_invoke: exceptions raised in a replay-thread callback must reach the caller."""

import unittest

from tests._stubs import install

install()
from renderdoc_extension.utils import invoke  # noqa: E402


def fake_block_invoke(controller="ctl"):
    """Like qrenderdoc's BlockInvoke: runs the callback and drops whatever it raises"""
    def block(callback):
        try:
            callback(controller)
        except BaseException:                       # noqa: BLE001
            pass
    return block


class GuardedInvoke(unittest.TestCase):
    def test_result_of_a_successful_callback_is_untouched(self):
        box = {}
        invoke.guarded_invoke(fake_block_invoke("c"), lambda c: box.update(got=c))
        self.assertEqual(box, {"got": "c"})

    def test_value_error_keeps_its_type_and_message(self):
        def cb(c):
            raise ValueError("Event 1 has no color0 target")
        with self.assertRaises(ValueError) as ctx:
            invoke.guarded_invoke(fake_block_invoke(), cb)
        self.assertEqual(str(ctx.exception), "Event 1 has no color0 target")

    def test_other_errors_become_runtime_errors_naming_the_original(self):
        def cb(c):
            raise KeyError("nope")
        with self.assertRaises(RuntimeError) as ctx:
            invoke.guarded_invoke(fake_block_invoke(), cb)
        self.assertIn("KeyError", str(ctx.exception))
        self.assertIn("nope", str(ctx.exception))

    def test_only_the_first_failure_is_reported(self):
        def cb(c):
            raise ValueError("first")
        with self.assertRaises(ValueError) as ctx:
            invoke.guarded_invoke(fake_block_invoke(), cb)
        self.assertEqual(str(ctx.exception), "first")


if __name__ == "__main__":
    unittest.main()
