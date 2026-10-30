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


def open_capture(name, local=False):
    path = CAPTURES.get(name, name)
    if not os.path.isfile(path):
        raise SystemExit("capture not found: %s" % path)
    bridge = RenderDocBridge()
    t = time.time()
    r = bridge.call("open_capture", {"capture_path": path, "use_remote": not local}, timeout=600)
    status = bridge.call("get_capture_status")
    ctx = bridge.call("get_replay_context")
    print("opened %s api=%s remote=%s device=%s (%.0fs)" % (
        r.get("filename"), status.get("api"), r.get("replay_remote"), ctx.get("remote_friendly_name"),
        time.time() - t), flush=True)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    local = "--local" in argv
    if not args or args[0] not in ("restart", "open", "all"):
        raise SystemExit(__doc__)
    cmd = args[0]
    if cmd in ("restart", "all"):
        restart()
    if cmd in ("open", "all"):
        open_capture(args[1] if len(args) > 1 else "vulkan", local)


if __name__ == "__main__":
    main(sys.argv[1:])
