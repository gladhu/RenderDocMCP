"""
Development loop helper: restart RenderDoc, deploy the extension, connect the phone, open a capture.

    .venv\\Scripts\\python.exe scripts\\dev_cycle.py restart            # kill qrenderdoc, install extension, start, wait for bridge
    .venv\\Scripts\\python.exe scripts\\dev_cycle.py open vulkan        # open a capture on the phone (remote replay)
    .venv\\Scripts\\python.exe scripts\\dev_cycle.py all vulkan         # restart + open
    .venv\\Scripts\\python.exe scripts\\dev_cycle.py open gles --local  # replay on this PC instead

Capture names: gles / vulkan (see CAPTURES) or a path to an .rdc file. The MCP server itself needs no
deployment here: the venv has the repo installed in editable mode, a new client session picks up the code.

Environment: RENDERDOC_DIR (default C:\\Program Files\\RenderDoc, the official build, see CLAUDE.md).
Exit code 0 = done, 1 = failed.
"""

import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from mcp_server.bridge.client import RenderDocBridge, RenderDocBridgeError  # noqa: E402

RENDERDOC_DIR = os.environ.get("RENDERDOC_DIR", r"C:\Program Files\RenderDoc")
CAPTURES = {
    "gles": r"E:\renderdocAI\8.057.001.108-plante优化.rdc",
    "vulkan": r"E:\renderdocAI\test-Vulkan_02.rdc",
}


def kill_renderdoc():
    subprocess.call(["taskkill", "/F", "/IM", "qrenderdoc.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):                                    # wait until the process is really gone
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq qrenderdoc.exe"], capture_output=True, text=True).stdout
        if "qrenderdoc.exe" not in out:
            return
        time.sleep(0.2)
    raise SystemExit("qrenderdoc.exe did not exit")


def install_extension():
    subprocess.check_call([sys.executable, os.path.join(REPO, "scripts", "install_extension.py")],
                          stdout=subprocess.DEVNULL)


def start_renderdoc():
    exe = os.path.join(RENDERDOC_DIR, "qrenderdoc.exe")
    flags = 0x00000008 | 0x00000200                        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe], cwd=RENDERDOC_DIR, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_bridge(bridge, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            bridge.call("ping", timeout=3)
            return
        except RenderDocBridgeError:
            time.sleep(1)
    raise SystemExit("the MCP bridge did not answer within %ds" % timeout)


def restart():
    bridge = RenderDocBridge()
    t = time.time()
    kill_renderdoc()
    install_extension()
    start_renderdoc()
    wait_bridge(bridge)
    print("RenderDoc restarted, extension installed, bridge up (%.0fs)" % (time.time() - t), flush=True)


def open_capture(name, local=False, timeout=600):
    path = CAPTURES.get(name, name)
    if not os.path.isfile(path):
        raise SystemExit("capture not found: %s" % path)
    bridge = RenderDocBridge()
    t = time.time()
    r = bridge.call("open_capture", {"capture_path": path, "use_remote": not local}, timeout=timeout)
    status = bridge.call("get_capture_status")
    ctx = bridge.call("get_replay_context")
    print("opened %s api=%s remote=%s device=%s (%.0fs)" % (
        r.get("filename"), status.get("api"), r.get("replay_remote"), ctx.get("remote_friendly_name"),
        time.time() - t), flush=True)


def on_phone(ctx):
    return bool(ctx.get("remote_connected") and not ctx.get("is_localhost") and ctx.get("capture_loaded")
                and not ctx.get("capture_local", True))


def responsive(bridge, timeout=8):
    try:
        bridge.call("ping", timeout=timeout)
        return True
    except RenderDocBridgeError:
        return False


def ensure(name, local=False):
    """
    Make sure RenderDoc answers and replays `name` on the phone, with the least work: nothing if it already does,
    open_capture if the phone connection or the capture is gone, a restart if RenderDoc does not answer at all.
    The phone-side server stops now and then; the next open then fails and RenderDoc sits in a modal
    "Error opening capture" dialog on its UI thread, after which only a restart helps (see TODO.md).
    """
    bridge = RenderDocBridge()
    path = CAPTURES.get(name, name)
    if not responsive(bridge):
        print("RenderDoc does not answer: restarting", flush=True)
        restart()
    else:
        try:
            ctx = bridge.call("get_replay_context", timeout=30)
            status = bridge.call("get_capture_status", timeout=30)
            if local or (on_phone(ctx) and os.path.normcase(status.get("filename") or "") == os.path.normcase(path)):
                return
        except RenderDocBridgeError:
            pass
    for attempt in (1, 2):
        try:
            open_capture(name, local, timeout=240)
            return
        except RenderDocBridgeError as e:
            print("opening %s failed (%s)" % (name, str(e)[:80]), flush=True)
            if attempt == 2:
                raise SystemExit("could not open %s on the phone after a restart" % name)
            restart()


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    local = "--local" in argv
    if not args or args[0] not in ("restart", "open", "all", "ensure"):
        raise SystemExit(__doc__)
    cmd = args[0]
    if cmd == "ensure":
        ensure(args[1] if len(args) > 1 else "vulkan", local)
        return
    if cmd in ("restart", "all"):
        restart()
    if cmd in ("open", "all"):
        open_capture(args[1] if len(args) > 1 else "vulkan", local)


if __name__ == "__main__":
    main(sys.argv[1:])
