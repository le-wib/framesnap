"""Download rife-ncnn-vulkan (MIT) and keep only what FrameSnap needs.

    python packaging/fetch_rife.py [macos|windows|ubuntu] [--dest vendor/rife] [--zip FILE]

The release zip (about 430 MB, all models) is checked against a pinned SHA-256, then
only the executable, the RIFE v4.6 model, the licence and, on Windows, the OpenMP
runtime are extracted (about 40 MB). Used by the build scripts and by CI.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "20221029"
URL = "https://github.com/nihui/rife-ncnn-vulkan/releases/download/%s/rife-ncnn-vulkan-%s-%%s.zip" % (VERSION, VERSION)
SHA256 = {
    "macos": "4a63a1f3c9c715773c57d2ee51df1b315ed20cd6c63103e45c483ecc4400b595",
    "windows": "d8e4d772d26cd8006ef0ad0bc82eb191b53c68677d1ae2f42506d74cbbbea606",
    "ubuntu": "1e2c7ee7fa7daa326542d50622f0afedc80cf6f1858bda411d16385ffa5cdf68",
}
MODEL = "rife-v4.6"


def detect_os() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "ubuntu"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("os", nargs="?", default=detect_os(), choices=sorted(SHA256))
    ap.add_argument("--dest", default=str(Path(__file__).resolve().parent.parent / "vendor" / "rife"))
    ap.add_argument("--zip", help="use an already downloaded zip instead of downloading")
    args = ap.parse_args()

    dest = Path(args.dest)
    exe = "rife-ncnn-vulkan.exe" if args.os == "windows" else "rife-ncnn-vulkan"
    if (dest / exe).exists() and (dest / MODEL / "flownet.param").exists():
        print("already present: %s" % dest)
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="rife-fetch-"))
    try:
        zpath = Path(args.zip) if args.zip else tmp / "rife.zip"
        if not args.zip:
            print("downloading %s" % (URL % args.os), flush=True)
            urllib.request.urlretrieve(URL % args.os, zpath)
        got = sha256_of(zpath)
        if got != SHA256[args.os]:
            print("ERROR: checksum mismatch\n expected %s\n got      %s" % (SHA256[args.os], got))
            return 1
        wanted_exact = {exe, "LICENSE", "README.md", "vcomp140.dll"}
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        n = 0
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                parts = info.filename.replace("\\", "/").split("/", 1)
                rel = parts[1] if len(parts) == 2 else parts[0]
                if rel in wanted_exact or rel.startswith(MODEL + "/"):
                    target = (dest / rel).resolve()
                    if root not in target.parents:     # "rife-v4.6/../../x": never outside dest
                        print("ERROR: unsafe path in the zip: %s" % info.filename)
                        return 1
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    n += 1
        (dest / exe).chmod(0o755)
        print("extracted %d files to %s" % (n, dest))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
