"""
Capture a frame from a debuggable Android app with the RenderDoc Vulkan layer, without the RenderDoc UI.

    python scripts/capture_android.py --package com.example.game --activity com.unity3d.player.UnityPlayerActivity \
        --out D:\\farm\\renderdocAI\\game_vulkan.rdc [--wait 15] [--serial SERIAL] [--renderdoc-dir DIR]

Why it is not just "launch with the layer": on Android 11+ an app with targetSdk >= 30 cannot see the
RenderDoc package (package visibility), so the loader logs "Debug layer app ... not installed", the layer is
not loaded, and Unity's Vulkan probe then falls back to OpenGL ES. For a *debuggable* app the Vulkan loader
also searches the app's own data directory, so the layer library is copied there with `run-as`. (A
non-debuggable app needs the layer baked into the APK, or a <queries> entry for the RenderDoc package.)

Steps: enable the GPU debug layer settings -> copy libVkLayer_GLES_RenderDoc.so into the app's data dir ->
(re)start the app -> adb-forward the layer's target-control socket -> trigger a capture -> copy the .rdc to
the PC -> restore the phone (settings deleted, layer file removed, app stopped, forward removed).

Needs adb on PATH and the RenderDoc build whose renderdoc.pyd matches this Python (pymodules\\renderdoc.pyd).
The RenderDoc remote-server app on the phone is not needed for capturing, only for replaying afterwards.
"""

import argparse
import os
import re
import subprocess
import sys
import time
import zipfile

DEFAULT_RENDERDOC_DIR = r"D:\Program Files\RenderDoc_AI_20260716\Release"
LAYER_APK = "plugins/android/org.renderdoc.renderdoccmd.arm64.apk"
LAYER_LIB_IN_APK = "lib/arm64-v8a/libVkLayer_GLES_RenderDoc.so"
LAYER_LIB = "libVkLayer_GLES_RenderDoc.so"
LAYER_NAME = "VK_LAYER_RENDERDOC_Capture"
TARGET_SOCKET = "renderdoc_38920"       # first target-control port of the layer (38920 + index)
SETTINGS = ("enable_gpu_debug_layers", "gpu_debug_app", "gpu_debug_layers", "gpu_debug_layer_app")


def adb(serial, *args, check=True):
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(args)
    env = dict(os.environ, MSYS_NO_PATHCONV="1")     # Git Bash would rewrite /data/... arguments
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if check and r.returncode != 0:
        raise SystemExit("adb %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return (r.stdout or "").strip()


def check_app(serial, package):
    flags = adb(serial, "shell", "dumpsys", "package", package)
    if "pkg=Package" not in flags and "versionName" not in flags:
        raise SystemExit("package %s is not installed on the device" % package)
    if "DEBUGGABLE" not in flags:
        raise SystemExit("%s is not debuggable: run-as is unavailable. Build it with Development Build / "
                         "android:debuggable=true, or bake the RenderDoc layer into the APK." % package)


def graphics_api_from_log(serial, package):
    pid = adb(serial, "shell", "pidof", package, check=False).split()
    if not pid:
        return None
    log = adb(serial, "logcat", "-d", "--pid=" + pid[0], check=False)
    m = re.findall(r"Unity\s*:\s*\[TestTextureFormat\] Graphics Device: (\w+)", log)
    if m:
        return m[-1]
    m = re.findall(r"Vulkan detection: (\d+)", log)
    return ("Vulkan" if m and m[-1] != "0" else "not Vulkan") if m else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", required=True)
    ap.add_argument("--activity", required=True, help="full activity class, e.g. com.unity3d.player.UnityPlayerActivity")
    ap.add_argument("--out", required=True, help="where to write the .rdc on this PC")
    ap.add_argument("--serial", default=None, help="adb serial (default: the only device)")
    ap.add_argument("--wait", type=float, default=15.0, help="seconds to let the app load before capturing")
    ap.add_argument("--renderdoc-dir", default=DEFAULT_RENDERDOC_DIR)
    ap.add_argument("--keep", action="store_true", help="leave settings / layer / app in place for further captures")
    args = ap.parse_args()

    rd_dir = args.renderdoc_dir
    layer_apk = os.path.join(rd_dir, *LAYER_APK.split("/"))
    for p in (layer_apk, os.path.join(rd_dir, "pymodules", "renderdoc.pyd")):
        if not os.path.isfile(p):
            raise SystemExit("not found: " + p)

    check_app(args.serial, args.package)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)

    tmp = os.path.join(os.environ.get("TEMP", "."), "rdlayer")
    os.makedirs(tmp, exist_ok=True)
    local_lib = os.path.join(tmp, LAYER_LIB)
    if not os.path.isfile(local_lib):
        with zipfile.ZipFile(layer_apk) as z, open(local_lib, "wb") as f:
            f.write(z.read(LAYER_LIB_IN_APK))

    app_dir = "/data/user/0/" + args.package
    forward_port = None
    previous = dict((k, adb(args.serial, "shell", "settings", "get", "global", k)) for k in SETTINGS)
    try:
        print("enable GPU debug layer for", args.package)
        adb(args.serial, "shell", "settings", "put", "global", "enable_gpu_debug_layers", "1")
        adb(args.serial, "shell", "settings", "put", "global", "gpu_debug_app", args.package)
        adb(args.serial, "shell", "settings", "put", "global", "gpu_debug_layers", LAYER_NAME)
        adb(args.serial, "shell", "settings", "delete", "global", "gpu_debug_layer_app")

        print("copy layer into", app_dir)
        adb(args.serial, "push", local_lib, "/data/local/tmp/" + LAYER_LIB)
        adb(args.serial, "shell", "run-as", args.package, "cp", "/data/local/tmp/" + LAYER_LIB, app_dir + "/" + LAYER_LIB)
        adb(args.serial, "shell", "rm", "/data/local/tmp/" + LAYER_LIB)

        adb(args.serial, "logcat", "-c")
        adb(args.serial, "shell", "am", "force-stop", args.package)
        time.sleep(1)
        adb(args.serial, "shell", "am", "start", "-n", args.package + "/" + args.activity)
        print("started, waiting %.0fs" % args.wait)
        time.sleep(args.wait)

        api = graphics_api_from_log(args.serial, args.package)
        print("graphics API seen in the app log:", api)
        if api and api != "Vulkan":
            raise SystemExit("the app is not running Vulkan (%s); the layer only captures Vulkan" % api)
        if TARGET_SOCKET not in adb(args.serial, "shell", "cat", "/proc/net/unix"):
            raise SystemExit("the layer's capture socket %s is missing: the RenderDoc layer did not load "
                             "(check `adb logcat | grep -i \"GraphicsEnvironment\\|renderdoc\"`)" % TARGET_SOCKET)

        forward_port = int(adb(args.serial, "forward", "tcp:0", "localabstract:" + TARGET_SOCKET))
        capture(rd_dir, forward_port, os.path.abspath(args.out))
    finally:
        if args.keep:
            print("--keep: phone left as is (layer file in %s, settings set)" % app_dir)
        else:
            print("restore the phone")
            for k in SETTINGS:
                if previous.get(k) in (None, "", "null"):
                    adb(args.serial, "shell", "settings", "delete", "global", k, check=False)
                else:
                    adb(args.serial, "shell", "settings", "put", "global", k, previous[k], check=False)
            adb(args.serial, "shell", "run-as", args.package, "rm", "-f", app_dir + "/" + LAYER_LIB, check=False)
            adb(args.serial, "shell", "am", "force-stop", args.package, check=False)
        if forward_port is not None:
            adb(args.serial, "forward", "--remove", "tcp:%d" % forward_port, check=False)


def capture(rd_dir, port, out):
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
