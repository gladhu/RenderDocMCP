"""
Target-control client that runs INSIDE the official qrenderdoc (`qrenderdoc.exe --python capture_client_ui.py`).

The official RenderDoc build has no stand-alone `renderdoc.pyd` (the module is embedded in qrenderdoc.exe), so
capture_android.py uses this script to trigger a capture when only that build is installed.

Inputs (environment): RDC_PORT (adb-forwarded target-control port), RDC_OUT (.rdc to write on this PC),
RDC_RESULT (JSON file written when done), RDC_TIMEOUT (seconds, default 90), RDC_NUM_FRAMES (default 1).
Output: RDC_RESULT = {"ok": bool, "api": str, "device_bytes": int, "bytes": int, "target": str, "error": str}.
The process exits itself afterwards (os._exit), so the caller does not have to close the window.
"""

import json
import os
import time
import traceback

result_path = os.environ["RDC_RESULT"]


def finish(result):
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f)
        f.flush()
        os.fsync(f.fileno())
    os._exit(0)


try:
    import renderdoc as rd

    port = int(os.environ["RDC_PORT"])
    out = os.environ["RDC_OUT"]
    timeout = float(os.environ.get("RDC_TIMEOUT", "90"))
    frames = int(os.environ.get("RDC_NUM_FRAMES", "1"))

    tc = rd.CreateTargetControl("localhost", port, "capture_android.py", True)
    if tc is None or not tc.Connected():
        finish({"ok": False, "error": "could not connect to the target control on localhost:%d" % port})

    result = {"ok": False, "target": str(tc.GetTarget()), "pid": int(tc.GetPID())}
    tc.TriggerCapture(frames)
    cap_id, deadline = None, time.time() + timeout
    while time.time() < deadline:
        m = tc.ReceiveMessage(None)
        t = m.type
        if t == rd.TargetControlMessageType.NewCapture:
            cap_id = m.newCapture.captureId
            result["api"] = str(m.newCapture.api)
            result["device_bytes"] = int(m.newCapture.byteSize)
            tc.CopyCapture(cap_id, out)
        elif t == rd.TargetControlMessageType.CaptureCopied:
            result["bytes"] = os.path.getsize(out)
            result["ok"] = True
            break
        elif t == rd.TargetControlMessageType.Disconnected:
            result["error"] = "the app disconnected before the capture finished"
            break
    else:
        result["error"] = "timed out waiting for the capture"
    if cap_id is not None:
        try:
            tc.DeleteCapture(cap_id)
        except Exception:
            pass
    try:
        tc.Shutdown()
    except Exception:
        pass
    finish(result)
except SystemExit:
    raise
except BaseException:
    finish({"ok": False, "error": traceback.format_exc()})
