"""Core logic of FrameSnap: probing, frame rate snapping, re-encoding.

Everything here depends only on an ffmpeg binary (no ffprobe needed) and the
standard library, so it can be tested without any GUI.

How a clip is analysed
----------------------
ffmpeg writes one line per video packet with its timestamp (``-f framecrc``,
stream copy, so it is fast). From those timestamps we get:

* the real average frame rate ``(frames - 1) / (last pts - first pts)``, which
  is what a human means by "this clip is 23.976 fps". It does NOT rely on the
  ``r_frame_rate`` header, which reports 60 on files whose timestamps sit on a
  60 Hz grid (the case that breaks Premiere and After Effects),
* whether the timestamps are perfectly uniform (already clean CFR),
* whether they follow a regular grid (``regular``) or wander (true VFR).

How a clip is converted
-----------------------
* ``copy``      already uniform at the target rate: copied untouched.
* ``retime``    rate is within the tolerance of the target and timestamps are
                regular: frame N is simply stamped N / target. No frame is
                added or dropped. Audio is time-stretched by the tiny ratio
                so sync is kept (23.976 to 24 is 0.1 percent).
* ``resample``  rate is far from the target (or timestamps are irregular):
                frames are duplicated or dropped according to their real
                timestamps, the duration is preserved.
* ``skip``      auto mode only: the rate is not close to 24, 25, 30 or 60.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Union

IS_WINDOWS = os.name == "nt"
FFMPEG_EXE = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"

STANDARD_RATES = (24, 25, 30, 60)
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
OUT_DIRNAME = "recoded"
LOG_NAME = "framesnap-log.txt"
DEFAULT_TOLERANCE = 0.025  # 2.5 percent
REGULAR_DRIFT_FRAMES = 0.75
ATEMPO_EPSILON = 2e-4


class FFmpegNotFound(RuntimeError):
    pass


class ProbeError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# ffmpeg location
# --------------------------------------------------------------------------

def subprocess_kwargs() -> dict:
    """Extra subprocess options. On Windows, never flash a console window."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


def bundle_roots() -> List[str]:
    """Folders where a packaged app (PyInstaller .app) keeps its extra files."""
    roots: List[str] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        roots.append(base)
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        contents = os.path.dirname(exe_dir)          # macOS: FrameSnap.app/Contents
        roots += [
            os.path.join(contents, "Resources"),
            os.path.join(contents, "Frameworks"),
            exe_dir,                                  # Windows and Linux onedir
            os.path.join(exe_dir, "_internal"),
        ]
    return roots


def find_ffmpeg() -> str:
    """Return the path of a usable ffmpeg executable."""
    candidates: List[Optional[str]] = []
    env = os.environ.get("FRAMESNAP_FFMPEG")
    if env:
        candidates.append(env)
    for root in bundle_roots():
        candidates.append(os.path.join(root, "bin", FFMPEG_EXE))
    try:
        import imageio_ffmpeg  # type: ignore

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    candidates += [
        shutil.which("ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            if not os.access(c, os.X_OK):
                try:
                    os.chmod(c, 0o755)
                except OSError:
                    continue
            return c
    raise FFmpegNotFound(
        "ffmpeg not found. Install it with 'brew install ffmpeg' or set FRAMESNAP_FFMPEG."
    )


# --------------------------------------------------------------------------
# data classes
# --------------------------------------------------------------------------

@dataclass
class Probe:
    frames: int
    fps: float            # real average frame rate
    duration: float       # seconds of video
    uniform: bool         # all frame intervals identical (within 1 tick)
    regular: bool         # timestamps stay close to an ideal grid
    drift_frames: float   # worst deviation from the ideal grid, in frames
    has_audio: bool
    width: int = 0
    height: int = 0


@dataclass
class Plan:
    action: str                 # copy | retime | resample | skip
    target: Optional[int]
    reason: str = ""
    atempo: float = 1.0


@dataclass
class Job:
    src: Path
    dst: Path
    out_root: Path
    rel: str
    variant: str = ""        # which output variant this job produces ("" = the default one)


@dataclass
class JobResult:
    job: Job
    status: str                 # done | copied | skipped | exists | error | cancelled
    message: str = ""
    fps: Optional[float] = None
    target: Optional[int] = None
    action: str = ""
    dups: int = 0            # held frames found (when a repair mode is on)
    repair: str = ""         # interpolate | cut_keep | cut_stretch when repaired
    backend: str = ""        # engine used to create the missing frames
    variant: str = ""        # off | interpolate | cut_keep | cut_stretch


# --------------------------------------------------------------------------
# frame rate helpers
# --------------------------------------------------------------------------

def nearest_standard(fps: float, rates: Iterable[int] = STANDARD_RATES) -> int:
    """Standard rate with the smallest relative error."""
    return min(rates, key=lambda r: abs(fps - r) / r)


def is_near(fps: float, target: int, tolerance: float = DEFAULT_TOLERANCE) -> bool:
    return abs(fps - target) / target <= tolerance


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

_STREAM_VIDEO = re.compile(r"Stream #\d+:\d+.*?: Video:.*?(\d{2,5})x(\d{2,5})")


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **subprocess_kwargs(),
    )


def probe_file(ffmpeg: str, path: Union[str, Path]) -> Probe:
    path = str(path)
    # 1) per packet timestamps of the first video stream
    r = _run([
        ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", path,
        "-map", "0:v:0", "-c", "copy", "-f", "framecrc", "-",
    ])
    if r.returncode != 0:
        msg = (r.stderr or "").strip().splitlines()
        raise ProbeError(msg[-1] if msg else "unreadable video")
    tb_num, tb_den = 1, 1
    rows = []
    for line in r.stdout.splitlines():
        if line.startswith("#tb"):
            m = re.match(r"#tb\s+\d+:\s*(\d+)/(\d+)", line)
            if m:
                tb_num, tb_den = int(m.group(1)), int(m.group(2))
        elif line and not line.startswith("#"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                try:
                    rows.append((int(parts[2]), int(parts[3])))
                except ValueError:
                    continue
    if len(rows) < 2:
        raise ProbeError("not enough video frames")
    rows.sort()
    pts = [p for p, _ in rows]
    tb = tb_num / tb_den
    n = len(pts)
    deltas = [pts[i + 1] - pts[i] for i in range(n - 1)]
    span = (pts[-1] - pts[0]) * tb
    if span <= 0:
        raise ProbeError("invalid timestamps")
    fps = (n - 1) / span
    duration = n / fps
    uniform = min(deltas) > 0 and (max(deltas) - min(deltas)) <= 1
    drift = max(abs((pts[i] - pts[0]) * tb - i / fps) for i in range(n)) * fps
    regular = drift <= REGULAR_DRIFT_FRAMES

    # 2) container info: audio presence and size
    info = _run([ffmpeg, "-nostdin", "-hide_banner", "-i", path]).stderr
    has_audio = bool(re.search(r"Stream #\d+:\d+.*?: Audio:", info))
    w = h = 0
    m = _STREAM_VIDEO.search(info)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    return Probe(n, fps, duration, uniform, regular, drift, has_audio, w, h)


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def make_plan(probe: Probe, mode: Union[str, int], tolerance: float = DEFAULT_TOLERANCE) -> Plan:
    """Decide what to do with a clip. ``mode`` is "auto" or a target rate."""
    if mode == "auto":
        target = nearest_standard(probe.fps)
        if not is_near(probe.fps, target, tolerance):
            return Plan("skip", None, "not close to 24, 25, 30 or 60 fps")
    else:
        target = int(mode)
        if target not in STANDARD_RATES:
            raise ValueError("unsupported target rate: %r" % (mode,))

    if probe.uniform and abs(probe.fps - target) / target < 1e-4:
        return Plan("copy", target, "already clean")

    if is_near(probe.fps, target, tolerance) and probe.regular:
        # Audio is only stretched when the source rate is exactly known, i.e.
        # perfectly uniform (23.976, 29.97, 59.94...). On a regular but
        # non uniform grid (24 fps stamped on a 60 Hz grid) the measured rate
        # is only an estimate, so the audio is left alone.
        atempo = target / probe.fps
        if (not probe.uniform or not probe.has_audio
                or abs(atempo - 1.0) < ATEMPO_EPSILON):
            atempo = 1.0
        return Plan("retime", target, "snap to %d fps" % target, atempo)

    return Plan("resample", target, "convert to %d fps" % target)


# --------------------------------------------------------------------------
# output variants: several results per clip, side by side
# --------------------------------------------------------------------------

# key: (file name suffix, sub folder used when several variants are written without suffix)
VARIANTS = {
    "off": ("_recode", "cadence"),
    "interpolate": ("_recode-inter", "inter"),
    "cut_keep": ("_recode-keep", "keep"),
    "cut_stretch": ("_recode-speed", "speed"),
}
VARIANT_ORDER = ("off", "interpolate", "cut_keep", "cut_stretch")


def variant_job(job: Job, key: str, use_suffix: bool, multiple: bool) -> Job:
    """The job writing variant ``key`` of ``job``.

    * suffix on:  <recoded>/<dir>/<name><suffix>.<ext>
    * suffix off, one variant:  <recoded>/<dir>/<name>.<ext>   (same name, easy relink)
    * suffix off, several variants: <recoded>/<variant folder>/<dir>/<name>.<ext>
      (so that two variants never write the same file)
    """
    suffix, folder = VARIANTS[key]
    rel = Path(job.rel)
    name = rel.stem + (suffix if use_suffix else "") + rel.suffix
    sub = Path(folder) if (multiple and not use_suffix) else Path()
    return Job(job.src, job.out_root / sub / rel.parent / name, job.out_root, job.rel, key)


# --------------------------------------------------------------------------
# job collection
# --------------------------------------------------------------------------

def _is_video(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")


def collect_jobs(paths: Iterable[Union[str, Path]]) -> List[Job]:
    """Expand files and folders into jobs. Output: <folder>/recoded/<same name>."""
    jobs: List[Job] = []
    seen = set()

    def add(job: Job) -> None:
        key = str(job.src.resolve())
        if key not in seen:
            seen.add(key)
            jobs.append(job)

    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file():
            if _is_video(p):
                out_root = p.parent / OUT_DIRNAME
                add(Job(p, out_root / p.name, out_root, p.name))
        elif p.is_dir():
            out_root = p / OUT_DIRNAME
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d.lower() != OUT_DIRNAME and not d.startswith(".")
                )
                for name in sorted(filenames):
                    f = Path(dirpath) / name
                    if _is_video(f):
                        rel = f.relative_to(p)
                        add(Job(f, out_root / rel, out_root, str(rel)))
    return jobs


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------

def build_command(ffmpeg: str, src: Path, dst: Path, plan: Plan,
                  preset: str = "slow", crf: int = 12) -> List[str]:
    t = int(plan.target)  # type: ignore[arg-type]
    vf = "setpts=N/(%d*TB)" % t if plan.action == "retime" else "fps=%d" % t
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y",
        "-i", str(src), "-map", "0:v:0", "-map", "0:a?",
        "-vf", vf, "-r", str(t), "-fps_mode", "cfr",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-x264-params", "keyint=%d:min-keyint=%d" % (t, t),
        "-video_track_timescale", str(t * 1000),
        "-map_metadata", "0",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
    ]
    if plan.atempo != 1.0:
        cmd += ["-af", "atempo=%.6f" % plan.atempo]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(dst)]
    return cmd


EventCallback = Callable[[Job, str, dict], None]


class Recoder:
    def __init__(self, ffmpeg: Optional[str] = None,
                 tolerance: float = DEFAULT_TOLERANCE,
                 preset: str = "slow", crf: int = 12,
                 skip_existing: bool = True, repair=None):
        self.repair = repair          # a dedupe.RepairOptions, or None for no repair
        self.ffmpeg = ffmpeg or find_ffmpeg()
        self.tolerance = tolerance
        self.preset = preset
        self.crf = crf
        self.skip_existing = skip_existing
        self._cancel = threading.Event()
        self._proc: Optional[subprocess.Popen] = None

    # control -------------------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def reset(self) -> None:
        self._cancel.clear()

    # single steps --------------------------------------------------------
    def probe(self, path: Union[str, Path]) -> Probe:
        return probe_file(self.ffmpeg, path)

    def plan(self, probe: Probe, mode: Union[str, int]) -> Plan:
        return make_plan(probe, mode, self.tolerance)

    def process(self, job: Job, mode: Union[str, int],
                on_progress: Optional[Callable[[float], None]] = None,
                overwrite: bool = False,
                flagged: Optional[List[int]] = None) -> JobResult:
        """Probe, plan and convert one job. Errors are returned, not raised."""
        try:
            if self._cancel.is_set():
                return JobResult(job, "cancelled")
            if (self.skip_existing and not overwrite
                    and job.dst.exists() and job.dst.stat().st_size > 0):
                return JobResult(job, "exists", "already in recoded folder")
            probe = self.probe(job.src)
            plan = self.plan(probe, mode)
            res = JobResult(job, "done", plan.reason, probe.fps, plan.target, plan.action)
            if plan.action == "skip":
                res.status = "skipped"
                return res
            job.dst.parent.mkdir(parents=True, exist_ok=True)
            if self.repair is not None:
                from . import dedupe

                if flagged is None:
                    flagged, _ = dedupe.analyse(self.ffmpeg, job.src, probe, plan,
                                                self.repair.sensitivity, self._cancel)
                res.dups = len(flagged)
                if flagged:
                    self._repair(job, probe, plan, res, flagged, on_progress)
                    return res
            if plan.action == "copy":
                shutil.copy2(job.src, job.dst)
                res.status = "copied"
                return res
            self._encode(job, probe, plan, on_progress)
            self._verify(job, probe, plan)
            return res
        except Cancelled:
            return JobResult(job, "cancelled")
        except Exception as exc:  # noqa: BLE001
            return JobResult(job, "error", str(exc))

    def _repair(self, job: Job, probe: Probe, plan: Plan, res: JobResult,
                flagged: List[int],
                on_progress: Optional[Callable[[float], None]]) -> None:
        from . import dedupe

        tmp = job.dst.with_name(job.dst.stem + ".part" + job.dst.suffix)
        try:
            report = dedupe.repair(self.ffmpeg, job.src, tmp, probe, plan, self.repair,
                                   self.preset, self.crf, on_progress, self._cancel, flagged)
            out = self.probe(tmp)
            expected = report.frames_out + report.placeholder_frames
            if (not out.uniform or abs(out.fps - plan.target) / plan.target > 1e-4  # type: ignore[operator]
                    or abs(out.frames - expected) > 1):
                raise RuntimeError("repaired output failed verification")
            os.replace(tmp, job.dst)
        except BaseException:
            _silent_remove(tmp)
            raise
        res.repair = self.repair.mode if self.repair.mode == "interpolate" else (
            "cut_" + self.repair.audio)
        res.backend = report.backend
        if self.repair.mode == "interpolate":
            what = "%d held frames interpolated" % report.repaired
            if report.skipped_cuts:
                what += ", %d left (scene cut)" % report.skipped_cuts
        else:
            what = "%d held frames removed" % len(flagged)
        res.message = "%s, %s" % (res.message, what) if res.message else what
        if report.fallback_reason and self.repair.mode == "interpolate":
            res.message += " (GPU unavailable: %s)" % report.fallback_reason[:80]
        if on_progress:
            on_progress(100.0)

    def _encode(self, job: Job, probe: Probe, plan: Plan,
                on_progress: Optional[Callable[[float], None]]) -> None:
        tmp = job.dst.with_name(job.dst.stem + ".part" + job.dst.suffix)
        expected = (probe.frames / plan.target) if plan.action == "retime" else probe.duration
        cmd = build_command(self.ffmpeg, job.src, tmp, plan, self.preset, self.crf)
        errors: List[str] = []
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            **subprocess_kwargs(),
        )
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        sec = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    if on_progress and expected > 0:
                        on_progress(max(0.0, min(99.0, sec / expected * 100.0)))
                elif line and "=" not in line.split(" ", 1)[0]:
                    errors.append(line)
            rc = self._proc.wait()
        finally:
            self._proc = None
        if self._cancel.is_set():
            _silent_remove(tmp)
            raise Cancelled()
        if rc != 0:
            _silent_remove(tmp)
            raise RuntimeError(errors[-1] if errors else "ffmpeg failed (code %d)" % rc)
        os.replace(tmp, job.dst)
        if on_progress:
            on_progress(100.0)

    def _verify(self, job: Job, probe: Probe, plan: Plan) -> None:
        out = self.probe(job.dst)
        if not out.uniform or abs(out.fps - plan.target) / plan.target > 1e-4:  # type: ignore[operator]
            _silent_remove(job.dst)
            raise RuntimeError("output is not a clean %s fps file" % plan.target)
        if plan.action == "retime" and abs(out.frames - probe.frames) > 1:
            _silent_remove(job.dst)
            raise RuntimeError("frame count changed during retime")

    # batch ---------------------------------------------------------------
    def run(self, jobs: List[Job], mode: Union[str, int],
            on_event: Optional[EventCallback] = None,
            overwrite: Optional[Iterable[Union[str, Path]]] = None,
            flags: Optional[dict] = None,
            variants: Optional[List[tuple]] = None,
            use_suffix: bool = False) -> List[JobResult]:
        """Process jobs in order.

        ``variants`` is a list of (key, RepairOptions or None): each clip is written once per
        variant (see ``variant_job`` for the file names). Without it, the single output uses
        ``self.repair``. Entries of ``overwrite`` are redone even if an output exists: either a
        source path (all variants) or "<source path>|<variant key>".
        """
        self.reset()
        force = {str(p) for p in (overwrite or [])}
        emit = on_event or (lambda *_: None)
        saved_repair = self.repair
        vlist = list(variants) if variants else [(None, self.repair)]
        multiple = len(vlist) > 1
        results: List[JobResult] = []
        try:
            for job in jobs:
                for vi, (key, opts) in enumerate(vlist):
                    vjob = variant_job(job, key, use_suffix, multiple) if key else job
                    meta = {"variant": key or "", "index": vi, "total": len(vlist)}
                    if self._cancel.is_set():
                        res = JobResult(vjob, "cancelled")
                    else:
                        self.repair = opts
                        emit(vjob, "start", dict(meta))
                        res = self.process(
                            vjob, mode,
                            lambda p, j=vjob, m=meta: emit(j, "progress", dict(m, pct=p)),
                            overwrite=(str(job.src) in force
                                       or "%s|%s" % (job.src, key) in force),
                            flagged=(flags or {}).get(str(job.src)))
                    res.variant = key or ""
                    results.append(res)
                    emit(vjob, "result", dict(meta, result=res))
        finally:
            self.repair = saved_repair
        write_logs(results)
        return results


def _silent_remove(p: Path) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


def write_logs(results: List[JobResult]) -> None:
    """Append one line per file to <recoded>/framesnap-log.txt."""
    by_root = {}
    for r in results:
        by_root.setdefault(r.job.out_root, []).append(r)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    for root, rs in by_root.items():
        try:
            root.mkdir(parents=True, exist_ok=True)
            with open(root / LOG_NAME, "a", encoding="utf-8") as fh:
                fh.write("# %s\n" % stamp)
                for r in rs:
                    fps = "%.3f" % r.fps if r.fps else "?"
                    extra = ""
                    if r.repair:
                        extra = "  [%s, %d held frames, %s]" % (r.repair, r.dups, r.backend or "n/a")
                    elif r.variant:
                        extra = "  [%s]" % r.variant
                    fh.write("%-9s %s fps to %s  %s  %s%s\n" % (
                        r.status, fps, r.target or "none", r.job.rel, r.message, extra))
        except OSError:
            pass
