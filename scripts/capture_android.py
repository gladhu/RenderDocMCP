"""
Capture a frame from a debuggable Android app with the RenderDoc layer (Vulkan or OpenGL ES), without the
RenderDoc UI having to launch the app.

    python scripts/capture_android.py --package com.example.game --api gles --out D:\\farm\\renderdocAI\\game_gles.rdc \
        [--activity com.example.game/.Main] [--wait 15] [--serial SERIAL] [--renderdoc-dir DIR]
    python scripts/capture_android.py --package com.example.game --api gles --out ... --attach   # app already running

Why it is not just "launch with the layer": on Android 11+ an app with targetSdk >= 30 cannot see the
RenderDoc package (package visibility), so the loader logs "Debug layer app ... not installed", the layer is
not loaded, and Unity's Vulkan probe then falls back to OpenGL ES. For a *debuggable* app the loader
also searches the app's own data directory, so the layer library is copied there with `run-as`. (A
non-debuggable app needs the layer baked into the APK, or a <queries> entry for the RenderDoc package.)

The same library (libVkLayer_GLES_RenderDoc.so) is both layers: it exports AndroidGLESLayer_Initialize /
AndroidGLESLayer_GetProcAddress for the Android GLES layer mechanism (Android 10+), selected with the setting
`gpu_debug_layers_gles` (value = the library file name), and VK_LAYER_RENDERDOC_Capture for Vulkan
(`gpu_debug_layers`). Only the layer of the API you ask for is enabled, so the app's own API choice (Unity's
Vulkan probe) is not disturbed.

Steps: enable the GPU debug layer settings -> copy the layer into the app's data dir -> (re)start the app ->
adb-forward the layer's target-control socket -> trigger a capture -> copy the .rdc to the PC -> restore the
phone (settings restored, layer file removed, app stopped, forward removed, screen locked).

The capture is taken with RenderDoc's target-control client. Two ways to run it:
  * a stand-alone `renderdoc.pyd` (custom builds: <renderdoc-dir>\\pymodules\\renderdoc.pyd), or
  * the official qrenderdoc.exe (no stand-alone module): scripts/capture_client_ui.py runs inside it via --python.
The RenderDoc remote-server app on the phone is not needed for capturing, only for replaying afterwards.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile

CANDIDATE_RENDERDOC_DIRS = (
    os.environ.get("RENDERDOC_DIR"),
    r"D:\Program Files\RenderDoc_AI_20260716\Release",
    r"C:\Program Files\RenderDoc",
)
LAYER_APK = "plugins/android/org.renderdoc.renderdoccmd.arm64.apk"
LAYER_LIB_IN_APK = "lib/arm64-v8a/libVkLayer_GLES_RenderDoc.so"
LAYER_LIB = "libVkLayer_GLES_RenderDoc.so"
VK_LAYER_NAME = "VK_LAYER_RENDERDOC_Capture"
TARGET_SOCKET = "renderdoc_38920"       # first target-control port of the layer (38920 + index)
SETTINGS = ("enable_gpu_debug_layers", "gpu_debug_app", "gpu_debug_layers", "gpu_debug_layers_gles",
            "gpu_debug_layer_app")
HERE = os.path.dirname(os.path.abspath(__file__))


def adb(serial, *args, check=True):
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(args)
    env = dict(os.environ, MSYS_NO_PATHCONV="1")     # Git Bash would rewrite /data/... arguments
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if check and r.returncode != 0:
        raise SystemExit("adb %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return (r.stdout or "").strip()


def find_renderdoc_dir(explicit):
    """The directory to use: an explicit one, else the first candidate that has a client we can drive."""
    if explicit:
        return explicit
    for d in CANDIDATE_RENDERDOC_DIRS:
        if d and os.path.isfile(os.path.join(d, *LAYER_APK.split("/"))):
            return d
    raise SystemExit("no RenderDoc install found (looked in %s); pass --renderdoc-dir" %
                     [d for d in CANDIDATE_RENDERDOC_DIRS if d])


def check_app(serial, package):
    flags = adb(serial, "shell", "dumpsys", "package", package)
    if "pkg=Package" not in flags and "versionName" not in flags:
        raise SystemExit("package %s is not installed on the device" % package)
    if "DEBUGGABLE" not in flags:
        raise SystemExit("%s is not debuggable: run-as is unavailable. Build it with Development Build / "
                         "android:debuggable=true, or bake the RenderDoc layer into the APK." % package)


def launcher_activity(serial, package):
    out = adb(serial, "shell", "cmd", "package", "resolve-activity", "--brief",
              "-c", "android.intent.category.LAUNCHER", package).splitlines()
    comp = out[-1].strip() if out else ""
    if "/" not in comp:
        raise SystemExit("could not resolve the launcher activity of %s; pass --activity" % package)
    return comp


def graphics_api_from_log(serial, package):
    """What the app says it renders with (None when it has not said yet); informational only."""
    pid = adb(serial, "shell", "pidof", package, check=False).split()
    if not pid:
        return None
    log = adb(serial, "logcat", "-d", "--pid=" + pid[0], check=False)
    m = re.findall(r"Unity\s*:\s*\[TestTextureFormat\] Graphics Device: (\w+)", log)
    if m:
        return m[-1]
    m = re.findall(r"Version: (OpenGL ES [\d.]+)", log)
    if m:
        return m[-1]
    m = re.findall(r"Vulkan detection: (\d+)", log)
    return ("Vulkan" if m and m[-1] != "0" else "not Vulkan") if m else None


def unlock(serial):
    """A dark/locked screen pauses Unity (nothing renders, so nothing can be captured). The phone has no password."""
    adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP", check=False)
    adb(serial, "shell", "wm", "dismiss-keyguard", check=False)
    adb(serial, "shell", "input", "swipe", "540", "1800", "540", "600", "200", check=False)


def lock(serial):
    adb(serial, "shell", "input", "keyevent", "KEYCODE_SLEEP", check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", required=True)
    ap.add_argument("--api", choices=("vulkan", "gles"), default="vulkan",
                    help="which RenderDoc layer to enable (default vulkan; the game must really use that API)")
    ap.add_argument("--activity", default=None,
                    help="activity to start, e.g. com.example.game/.Main or a full class name "
                         "(default: the launcher activity)")
    ap.add_argument("--unity-args", default=None, help="Unity command line for the start, e.g. '-perfport 9210'")
    ap.add_argument("--out", default=None, help="where to write the .rdc on this PC (required unless --setup-only / --cleanup)")
    ap.add_argument("--serial", default=None, help="adb serial (default: the only device)")
    ap.add_argument("--wait", type=float, default=15.0, help="seconds to let the app load before capturing")
    ap.add_argument("--renderdoc-dir", default=None)
    ap.add_argument("--keep", action="store_true", help="leave settings / layer / app in place for further captures")
    ap.add_argument("--attach", action="store_true",
                    help="the app is already running with the layer loaded (started with --keep): only capture")
    ap.add_argument("--setup-only", action="store_true",
                    help="enable the layer and start the app, then stop: drive the game to the scene you want, then run "
                         "again with --attach (capture) and --cleanup (restore the phone)")
    ap.add_argument("--cleanup", action="store_true", help="restore the phone after --setup-only / --attach; no capture")
    ap.add_argument("--no-lock", action="store_true", help="do not turn the screen off afterwards")
    args = ap.parse_args()

    rd_dir = find_renderdoc_dir(args.renderdoc_dir)
    layer_apk = os.path.join(rd_dir, *LAYER_APK.split("/"))
    if not os.path.isfile(layer_apk):
        raise SystemExit("not found: " + layer_apk)
    pyd = os.path.join(rd_dir, "pymodules", "renderdoc.pyd")
    qrd = os.path.join(rd_dir, "qrenderdoc.exe")
    if not os.path.isfile(pyd) and not os.path.isfile(qrd):
        raise SystemExit("neither %s nor %s exists: no way to run a target-control client" % (pyd, qrd))

    check_app(args.serial, args.package)
    capturing = not (args.setup_only or args.cleanup)
    if capturing and not args.out:
        raise SystemExit("--out is required to capture")
    out = os.path.abspath(args.out) if args.out else None
    if out:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out):
            os.remove(out)                   # so a stale file can never pass for a new capture

    app_dir = "/data/user/0/" + args.package
    forward_port = None
    # The settings as they were BEFORE the first setup. --setup-only / --cleanup are separate runs, so they are kept in a file
    # (not overwritten by a second setup, or the cleanup would "restore" our own values).
    state_path = os.path.join(tempfile.gettempdir(), "rdc_capture_state_%s.json" % (args.serial or "default"))
    if os.path.isfile(state_path):
        with open(state_path, encoding="utf-8") as f:
            previous = json.load(f)
    else:
        previous = dict((k, adb(args.serial, "shell", "settings", "get", "global", k)) for k in SETTINGS)
    # The restore is owed as soon as the settings are touched, whatever happens next; --setup-only / --attach hand it to --cleanup.
    deferred = args.keep or args.attach or args.setup_only
    try:
        unlock(args.serial)
        if not args.attach and not args.cleanup:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(previous, f)
            tmp = os.path.join(tempfile.gettempdir(), "rdlayer")
            os.makedirs(tmp, exist_ok=True)
            local_lib = os.path.join(tmp, LAYER_LIB)
            with zipfile.ZipFile(layer_apk) as z:      # re-extract every time: the layer must match the client
                with open(local_lib, "wb") as f:
                    f.write(z.read(LAYER_LIB_IN_APK))

            print("enable the %s layer for %s" % (args.api, args.package))
            adb(args.serial, "shell", "settings", "put", "global", "enable_gpu_debug_layers", "1")
            adb(args.serial, "shell", "settings", "put", "global", "gpu_debug_app", args.package)
            adb(args.serial, "shell", "settings", "delete", "global", "gpu_debug_layer_app")
            if args.api == "gles":
                adb(args.serial, "shell", "settings", "put", "global", "gpu_debug_layers_gles", LAYER_LIB)
                adb(args.serial, "shell", "settings", "delete", "global", "gpu_debug_layers")
            else:
                adb(args.serial, "shell", "settings", "put", "global", "gpu_debug_layers", VK_LAYER_NAME)
                adb(args.serial, "shell", "settings", "delete", "global", "gpu_debug_layers_gles")

            print("copy the layer into", app_dir)
            adb(args.serial, "push", local_lib, "/data/local/tmp/" + LAYER_LIB)
            adb(args.serial, "shell", "run-as", args.package, "cp", "/data/local/tmp/" + LAYER_LIB,
                app_dir + "/" + LAYER_LIB)
            adb(args.serial, "shell", "rm", "/data/local/tmp/" + LAYER_LIB)

            activity = args.activity or launcher_activity(args.serial, args.package)
            if "/" not in activity:
                activity = args.package + "/" + activity
            adb(args.serial, "logcat", "-c")
            adb(args.serial, "shell", "am", "force-stop", args.package)
            time.sleep(1)
            start = ["shell", "am", "start", "-n", activity]
            if args.unity_args:
                start += ["-e", "unity", "'%s'" % args.unity_args]   # one quoted string, or the device shell splits it
            adb(args.serial, *start)
            print("started %s, waiting %.0fs" % (activity, args.wait))
            time.sleep(args.wait)
            unlock(args.serial)

        if capturing:
            api = graphics_api_from_log(args.serial, args.package)
            print("graphics API seen in the app log:", api)
            if api and args.api == "vulkan" and api != "Vulkan":
                raise SystemExit("the app is not running Vulkan (%s); the Vulkan layer only captures Vulkan" % api)
            if api and args.api == "gles" and api.startswith("Vulkan"):
                raise SystemExit("the app is running Vulkan; --api gles would capture nothing")
            if TARGET_SOCKET not in adb(args.serial, "shell", "cat", "/proc/net/unix"):
                raise SystemExit(
                    "the layer's capture socket %s is missing: the RenderDoc %s layer did not load "
                    "(check `adb logcat | grep -iE GraphicsEnvironment.renderdoc.GLES`)"
                    % (TARGET_SOCKET, args.api))

            forward_port = int(adb(args.serial, "forward", "tcp:0", "localabstract:" + TARGET_SOCKET))
            if os.path.isfile(pyd):
                capture_with_pyd(rd_dir, forward_port, out)
            else:
                capture_with_qrenderdoc(qrd, rd_dir, forward_port, out)
    finally:
        if forward_port is not None:
            adb(args.serial, "forward", "--remove", "tcp:%d" % forward_port, check=False)
        if deferred and not args.cleanup:
            print("phone left as is (layer + settings stay until --cleanup)")
        else:
            print("restore the phone")
            for k in SETTINGS:
                if previous.get(k) in (None, "", "null"):
                    adb(args.serial, "shell", "settings", "delete", "global", k, check=False)
                else:
                    adb(args.serial, "shell", "settings", "put", "global", k, previous[k], check=False)
            adb(args.serial, "shell", "run-as", args.package, "rm", "-f", app_dir + "/" + LAYER_LIB, check=False)
            adb(args.serial, "shell", "am", "force-stop", args.package, check=False)
            if os.path.isfile(state_path):
                os.remove(state_path)
            if not args.no_lock:
                lock(args.serial)


def capture_with_qrenderdoc(qrd, rd_dir, port, out, timeout=90):
    """Run scripts/capture_client_ui.py inside the official qrenderdoc and read its result file."""
    result_path = os.path.join(tempfile.gettempdir(), "rdc_capture_result_%d.json" % os.getpid())
    if os.path.exists(result_path):
        os.remove(result_path)
    env = dict(os.environ, RDC_PORT=str(port), RDC_OUT=out, RDC_RESULT=result_path, RDC_TIMEOUT=str(timeout))
    proc = subprocess.Popen([qrd, "--python", os.path.join(HERE, "capture_client_ui.py")], cwd=rd_dir, env=env)
    deadline = time.time() + timeout + 60            # qrenderdoc start-up + the capture itself
    try:
        while time.time() < deadline and not os.path.isfile(result_path):
            if proc.poll() is not None and not os.path.isfile(result_path):
                raise SystemExit("qrenderdoc exited (code %s) without writing a result" % proc.returncode)
            time.sleep(0.5)
        if not os.path.isfile(result_path):
            raise SystemExit("timed out waiting for qrenderdoc to finish the capture")
        time.sleep(0.5)
        with open(result_path, encoding="utf-8") as f:
            res = json.load(f)
    finally:
        if proc.poll() is None:
            proc.terminate()
        if os.path.exists(result_path):
            os.remove(result_path)
    if not res.get("ok"):
        raise SystemExit("capture failed: %s" % res.get("error"))
    print("connected to %s (pid %s)" % (res.get("target"), res.get("pid")))
    print("captured frame %s: %s bytes on the device" % (res.get("api"), res.get("device_bytes")))
    print("saved", out, res.get("bytes"), "bytes")


def capture_with_pyd(rd_dir, port, out):
    os.add_dll_directory(rd_dir)
    sys.path.insert(0, os.path.join(rd_dir, "pymodules"))
    os.chdir(rd_dir)
    import renderdoc as rd

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    tc = rd.CreateTargetControl("localhost", port, "capture_android.py", True)
    if tc is None or not tc.Connected():
        raise SystemExit("could not connect to the layer's target control on localhost:%d" % port)
    print("connected to %s (pid %s)" % (tc.GetTarget(), tc.GetPID()))
    tc.TriggerCapture(1)
    cap_id, deadline = None, time.time() + 90
    while time.time() < deadline:
        m = tc.ReceiveMessage(None)
        t = m.type
        if t == rd.TargetControlMessageType.NewCapture:
            cap_id = m.newCapture.captureId
            print("captured frame %s: %d bytes on the device" % (m.newCapture.api, m.newCapture.byteSize))
            tc.CopyCapture(cap_id, out)
        elif t == rd.TargetControlMessageType.CaptureCopied:
            print("saved", out, os.path.getsize(out), "bytes")
            break
        elif t == rd.TargetControlMessageType.Disconnected:
            raise SystemExit("the app disconnected before the capture finished")
    else:
        raise SystemExit("timed out waiting for the capture")
    if cap_id is not None:
        tc.DeleteCapture(cap_id)
    tc.Shutdown()
    rd.ShutdownReplay()


if __name__ == "__main__":
    main()
