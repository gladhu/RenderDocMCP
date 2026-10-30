"""
Mali Offline Compiler installer.

Downloads Arm Performance Studio from Arm's official artifact server and unpacks only
mali_offline_compiler into <repo>/tools/malioc (git-ignored). mcp_server/malioc.py looks there.
No system-wide install, no admin rights needed (Windows: msiexec /a).

The Arm license does not allow redistributing Arm Tools, so the binaries are not committed:
every user runs this script once.

    python scripts/install_malioc.py [--version 2026.5] [--msi path/to/local.msi]
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEST = PROJECT_ROOT / "tools" / "malioc"
BASE_URL = "https://artifacts.tools.arm.com/arm-performance-studio/%(v)s/Arm_Performance_Studio_%(v)s_windows_x86-64.msi"


def download(url, target):
    print("Downloading %s" % url)
    last = [-1]

    def hook(blocks, block_size, total):
        if total > 0:
            pct = min(100, blocks * block_size * 100 // total)
            if pct // 5 != last[0]:
                last[0] = pct // 5
                print("  %d%% of %.0f MB" % (pct, total / 1e6))

    urllib.request.urlretrieve(url, str(target), hook)


def install(version, msi):
    if sys.platform != "win32":
        print("Only Windows is automated. On Linux/macOS install Arm Performance Studio and set MALIOC_PATH.")
        return 1

    # Not %TEMP%: its 8.3 short path makes msiexec /a fail with 1603
    DEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="_tmp_", dir=str(DEST.parent)))
    try:
        if msi is None:
            msi = tmp / "Arm_PS.msi"
            download(BASE_URL % {"v": version}, msi)
        extract = tmp / "extract"
        print("Unpacking %s" % msi)
        subprocess.run(
            ["msiexec", "/a", str(Path(msi).resolve()), "/qn", "TARGETDIR=%s" % extract],
            check=True,
        )
        hits = list(extract.rglob("mali_offline_compiler"))
        if not hits or not (hits[0] / "malioc.exe").is_file():
            print("Error: mali_offline_compiler not found in the package")
            return 1
        if DEST.exists():
            shutil.rmtree(str(DEST))
        DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(hits[0]), str(DEST))
        # License text must stay with the tool
        lic = hits[0].parent.parent / "license_terms"
        if lic.is_dir():
            shutil.copytree(str(lic), str(DEST / "license_terms"))
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    exe = DEST / "malioc.exe"
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
    print("Installed: %s" % exe)
    print((out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else "")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", default="2026.5", help="Arm Performance Studio release (default 2026.5)")
    parser.add_argument("--msi", help="use an already downloaded installer instead of downloading")
    args = parser.parse_args()
    sys.exit(install(args.version, args.msi))


if __name__ == "__main__":
    main()
