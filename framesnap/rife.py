"""GPU frame interpolation with rife-ncnn-vulkan (RIFE v4.6, MIT license).

ncnn runs the neural network through Vulkan:

* Windows: the Vulkan driver of the graphics card (NVIDIA, AMD or Intel). CUDA
  is not needed; NVIDIA cards use their Vulkan driver, tensor cores included.
* macOS: the Apple GPU (Apple Silicon or AMD) through Metal, using the
  MoltenVK layer that is built into the macOS binary.

The command line tool is bundled with the app. If it is missing, or if no real
GPU is available (virtual machine, software Vulkan), the caller falls back to
the CPU optical flow in ``dedupe``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from .core import IS_WINDOWS, bundle_roots, popen, release

RIFE_EXE = "rife-ncnn-vulkan.exe" if IS_WINDOWS else "rife-ncnn-vulkan"
MODEL_NAME = "rife-v4.6"
SOFTWARE_DEVICES = ("llvmpipe", "lavapipe", "swiftshader", "basic render", "software")
CALL_TIMEOUT = 180
GPU_CHECK_TIMEOUT = 45


class RifeError(RuntimeError):
    pass


def find_rife() -> Optional[Path]:
    """Path of the rife-ncnn-vulkan executable, or None."""
    candidates: List[Path] = []
    env = os.environ.get("FRAMESNAP_RIFE")
    if env:
        candidates.append(Path(env))
    for root in bundle_roots():
        candidates.append(Path(root) / "rife" / RIFE_EXE)
    here = Path(__file__).resolve().parent
    candidates.append(here.parent / "vendor" / "rife" / RIFE_EXE)
    which = shutil.which("rife-ncnn-vulkan")
    if which:
        candidates.append(Path(which))
    for c in candidates:
        if c.is_file() and (c.parent / MODEL_NAME / "flownet.param").exists():
            if not os.access(c, os.X_OK):
                try:
                    os.chmod(c, 0o755)
                except OSError:
                    continue
            return c
    return None


def vulkan_runtime_present() -> bool:
    """Windows: rife-ncnn-vulkan.exe cannot even start without vulkan-1.dll (installed with
    the graphics driver). Checking first avoids a "DLL not found" system dialog."""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        ctypes.WinDLL("vulkan-1.dll")  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


def _extra_args() -> List[str]:
    # Apple Silicon: more than one load/process/save thread is not supported
    if sys.platform == "darwin":
        return ["-j", "1:1:1"]
    return []


def _device_from_log(text: str) -> str:
    m = re.search(r"\[\d+ ([^\]]+)\]\s+queueC", text)
    return m.group(1).strip() if m else ""


def is_software_device(name: str) -> bool:
    low = name.lower()
    return any(s in low for s in SOFTWARE_DEVICES)


def _quiet_windows_errors() -> None:
    """Windows: do not pop up a system box when rife cannot load vulkan-1.dll (no graphics
    driver, virtual machine). The child process then simply fails and we fall back to the CPU."""
    if IS_WINDOWS:
        try:
            import ctypes

            # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


class Rife:
    def __init__(self, exe: Optional[Path] = None, timeout: int = CALL_TIMEOUT):
        self.exe = Path(exe) if exe else find_rife()
        if self.exe is None:
            raise RifeError("rife-ncnn-vulkan not found")
        self.model = self.exe.parent / MODEL_NAME
        self.device = ""
        self.timeout = timeout
        _quiet_windows_errors()

    def _run(self, a_path: Path, b_path: Path, out: Path, t: float) -> None:
        if not vulkan_runtime_present():
            raise RifeError("no Vulkan driver found (vulkan-1.dll): install the graphics driver")
        cmd = [str(self.exe), "-m", str(self.model), "-0", str(a_path), "-1", str(b_path),
               "-o", str(out), "-s", "%.4f" % t] + _extra_args()
        try:
            proc = popen(cmd, cwd=str(self.exe.parent), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RifeError("rife could not run: %s" % exc)
        try:
            try:
                out_text, err_text = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise RifeError("rife could not run: no answer after %d s" % self.timeout)
        finally:
            release(proc)
        r = subprocess.CompletedProcess(cmd, proc.returncode, out_text, err_text)
        log = (r.stderr or "") + (r.stdout or "")
        if not self.device:
            self.device = _device_from_log(log)
        if r.returncode != 0 or not out.exists():
            tail = [l for l in log.splitlines() if l.strip()][-1:] or ["no output"]
            raise RifeError("rife failed (code %s): %s" % (r.returncode, tail[0]))

    def interpolate(self, a: np.ndarray, b: np.ndarray, ts: Sequence[float]) -> List[np.ndarray]:
        """Frames between BGR frames ``a`` and ``b`` at each time t in (0, 1)."""
        import cv2

        work = Path(tempfile.mkdtemp(prefix="framesnap-rife-"))
        try:
            pa, pb = work / "a.png", work / "b.png"
            params = [cv2.IMWRITE_PNG_COMPRESSION, 1]
            cv2.imwrite(str(pa), a, params)
            cv2.imwrite(str(pb), b, params)
            out: List[np.ndarray] = []
            for i, t in enumerate(ts):
                po = work / ("o%d.png" % i)
                self._run(pa, pb, po, t)
                img = cv2.imread(str(po), cv2.IMREAD_COLOR)
                if img is None or img.shape != a.shape:
                    raise RifeError("unexpected rife output")
                out.append(img)
            return out
        finally:
            shutil.rmtree(work, ignore_errors=True)


_check_cache: Dict[str, dict] = {}


def gpu_check(force: bool = False) -> dict:
    """Is a real GPU usable? Returns {available, ok, device, software, seconds, error}.

    ``ok`` means RIFE ran. ``software`` means Vulkan is emulated on the CPU (very slow,
    so it must not be used). The result is cached.
    """
    if "r" in _check_cache and not force:
        return _check_cache["r"]
    res = {"available": False, "ok": False, "device": "", "software": False,
           "seconds": 0.0, "error": ""}
    exe = find_rife()
    if exe is None:
        res["error"] = "rife-ncnn-vulkan not found"
        _check_cache["r"] = res
        return res
    res["available"] = True
    if not vulkan_runtime_present():
        res["error"] = "no Vulkan driver found (vulkan-1.dll)"
        _check_cache["r"] = res
        return res
    try:
        import cv2

        rife = Rife(exe, timeout=GPU_CHECK_TIMEOUT)
        a = np.zeros((128, 128, 3), np.uint8)
        b = np.zeros((128, 128, 3), np.uint8)
        cv2.rectangle(a, (20, 40), (50, 70), (255, 255, 255), -1)
        cv2.rectangle(b, (60, 40), (90, 70), (255, 255, 255), -1)
        t0 = time.time()
        rife.interpolate(a, b, [0.5])
        res["seconds"] = time.time() - t0
        res["device"] = rife.device
        res["software"] = is_software_device(rife.device)
        res["ok"] = True
    except Exception as exc:  # noqa: BLE001
        res["error"] = str(exc)
    _check_cache["r"] = res
    return res


def gpu_usable() -> bool:
    r = gpu_check()
    return bool(r["ok"] and not r["software"])
