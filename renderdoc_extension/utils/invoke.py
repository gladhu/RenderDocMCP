"""
Run a callback on the replay thread and surface what it raises.

BlockInvoke swallows exceptions raised inside the callback: the call returns normally and whatever the callback
should have filled in stays empty, so a tool whose lookup failed (an event without a render target, a texture that
does not exist, ...) answered null instead of an error. guarded_invoke catches the exception on the replay thread
and raises it again on the calling thread.
"""

import traceback


def guarded_invoke(block_invoke, callback):
    failures = []

    def guarded(controller):
        try:
            callback(controller)
        except Exception as e:                       # noqa: BLE001 - re-raised below
            traceback.print_exc()
            failures.append(e)

    block_invoke(guarded)
    if failures:
        e = failures[0]
        if isinstance(e, ValueError):               # request errors keep their own message (-32602)
            raise e
        raise RuntimeError("%s: %s" % (type(e).__name__, e))
