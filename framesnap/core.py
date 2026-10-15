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

Child processes
---------------
Every long running ffmpeg (or rife) process is started with ``popen`` and kept in
a registry, so that ``kill_all`` can stop them when the app closes: worker
threads are daemon threads and would otherwise leave encoders running in the
background. ``kill_all`` is also registered with ``atexit``.
"""

from __future__ import annotations

import atexit
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Union

IS_WINDOWS = os.name == "nt"
FFMPEG_EXE = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"

STANDARD_RATES = (24, 25, 30, 60)
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
OUT_DIRNAME = "recoded"
LOG_NAME = "framesnap-log.txt"
MANIFEST_NAME = ".framesnap-manifest.json"
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
# child processes
# --------------------------------------------------------------------------

def subprocess_kwargs() -> dict:
    """Extra subprocess options. On Windows, never flash a console window."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


_PROCS: "set[subprocess.Popen]" = set()
_PROCS_LOCK = threading.Lock()


def popen(cmd: List[str], **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` that registers the child so that ``kill_all`` can stop it."""
    proc = subprocess.Popen(cmd, **kwargs, **subprocess_kwargs())
    with _PROCS_LOCK:
        _PROCS.add(proc)
    return proc


def release(proc: Optional[subprocess.Popen]) -> None:
    """Forget a finished child (call it in a ``finally`` after ``wait``)."""
    if proc is not None:
        with _PROCS_LOCK:
            _PROCS.discard(proc)


def live_processes() -> List[subprocess.Popen]:
    with _PROCS_LOCK:
        procs = list(_PROCS)
    return [p for p in procs if p.poll() is None]


def kill_all(timeout: float = 5.0) -> int:
    """Kill every registered child that is still running. Returns how many were killed."""
    with _PROCS_LOCK:
        procs = list(_PROCS)
    killed = 0
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
                killed += 1
            except OSError:
                pass
    for p in procs:
        try:
            p.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass
    with _PROCS_LOCK:
        _PROCS.difference_update(procs)
    return killed


atexit.register(kill_all)


def run_capture(cmd: List[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """Run a short command, capture its text output. Registered like any other child."""
    proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, encoding="utf-8", errors="replace")
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    finally:
        release(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


class StderrTail:
    """Drain a child's stderr in the background and keep its last lines.

    A pipe that nobody reads fills up (about 64 KB) and then blocks the child for
    ever: a damaged file makes ffmpeg print thousands of decoding errors, which
    used to freeze the analysis and the repair.
    """

    def __init__(self, stream, keep: int = 40):
        self.lines: "collections.deque[str]" = collections.deque(maxlen=keep)
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self.lines.append(line)
        except (OSError, ValueError):
            pass

    def join(self, timeout: float = 5.0) -> None:
        self._thread.join(timeout)

    def last(self, default: str = "") -> str:
        return self.lines[-1] if self.lines else default


# --------------------------------------------------------------------------
# ffmpeg location
# --------------------------------------------------------------------------

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
    width: int = 0        # size of the DECODED frames: rotation already applied
    height: int = 0
    rotation: int = 0     # display rotation of the file (0, 90, 180, 270), applied by ffmpeg
    pix_fmt: str = ""
    color: Dict[str, str] = field(default_factory=dict)   # range, space, primaries, trc


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
_PIX_PROPS = re.compile(r",\s*([a-z0-9_]+)\(([^)]*)\)")
_PIX_PLAIN = re.compile(r",\s*(yuvj?\d{3}p\w*|nv12|nv21|p010le|gray\w*|rgb\w*|bgr\w*)\s*,")
_ROTATION = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees")

# names printed by ffmpeg that its command line accepts back (anything else is ignored)
COLOR_SPACES = {"bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "ycgco", "bt2020nc",
                "bt2020c", "smpte2085", "chroma-derived-nc", "chroma-derived-c", "ictcp"}
COLOR_PRIMARIES = {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "bt2020",
                   "smpte428", "smpte431", "smpte432", "jedec-p22", "ebu3213"}
COLOR_TRCS = {"bt709", "gamma22", "gamma28", "smpte170m", "smpte240m", "linear", "log100",
              "log316", "iec61966-2-4", "bt1361e", "iec61966-2-1", "bt2020-10", "bt2020-12",
              "smpte2084", "smpte428", "arib-std-b67"}


def _video_section(info: str) -> str:
    """Text of the first video stream in ``ffmpeg -i`` output (its line and side data)."""
    m = re.search(r"Stream #\d+:\d+.*?: Video:", info)
    if not m:
        return ""
    rest = info[m.start():]
    nxt = re.search(r"\n\s*Stream #", rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest


def parse_video_info(info: str) -> dict:
    """Size, rotation, pixel format and colour tags of the first video stream."""
    section = _video_section(info)
    first_line = section.splitlines()[0] if section else ""
    out: dict = {"width": 0, "height": 0, "rotation": 0, "pix_fmt": "", "color": {}}
    m = _STREAM_VIDEO.search(first_line)
    if m:
        out["width"], out["height"] = int(m.group(1)), int(m.group(2))
    r = _ROTATION.search(section)
    if r:
        out["rotation"] = int(round(float(r.group(1)))) % 360
    after_codec = first_line.split("Video:", 1)[-1]
    p = _PIX_PROPS.search(after_codec)
    if p and not p.group(1).startswith("0x"):
        out["pix_fmt"] = p.group(1)
        color: Dict[str, str] = {}
        for item in (x.strip() for x in p.group(2).split(",")):
            if item in ("tv", "pc"):
                color["range"] = item
            elif "/" in item:
                parts = item.split("/")
                if len(parts) == 3:
                    if parts[0] in COLOR_SPACES:
                        color["space"] = parts[0]
                    if parts[1] in COLOR_PRIMARIES:
                        color["primaries"] = parts[1]
                    if parts[2] in COLOR_TRCS:
                        color["trc"] = parts[2]
            elif item in COLOR_SPACES and item in COLOR_PRIMARIES and item in COLOR_TRCS:
                color.update(space=item, primaries=item, trc=item)   # "bt709" means all three
            elif item in COLOR_SPACES:
                color["space"] = item
        out["color"] = color
    else:
        q = _PIX_PLAIN.search(after_codec + ",")
        if q:
            out["pix_fmt"] = q.group(1)
    return out


def output_color(probe: Probe) -> Dict[str, str]:
    """Colour tags of frames decoded to 8 bit ``yuv420p``.

    ffmpeg converts the full range "j" formats (yuvj420p...) to limited range when it
    outputs yuv420p, so their output is tagged ``tv``; any other range is kept.
    """
    color = dict(probe.color or {})
    if probe.pix_fmt.startswith("yuvj"):
        color["range"] = "tv"
    return color


def color_args(color: Dict[str, str]) -> List[str]:
    """ffmpeg output options that write the colour tags into the file."""
    args: List[str] = []
    if color.get("range") in ("tv", "pc"):
        args += ["-color_range", color["range"]]
    if color.get("space") in COLOR_SPACES:
        args += ["-colorspace", color["space"]]
    if color.get("primaries") in COLOR_PRIMARIES:
        args += ["-color_primaries", color["primaries"]]
    if color.get("trc") in COLOR_TRCS:
        args += ["-color_trc", color["trc"]]
    return args


def setparams_filter(color: Dict[str, str]) -> str:
    """``setparams`` filter that tags raw frames without converting them."""
    parts = []
    if color.get("range") in ("tv", "pc"):
        parts.append("range=%s" % color["range"])
    if color.get("primaries") in COLOR_PRIMARIES:
        parts.append("color_primaries=%s" % color["primaries"])
    if color.get("trc") in COLOR_TRCS:
        parts.append("color_trc=%s" % color["trc"])
    if color.get("space") in COLOR_SPACES:
        parts.append("colorspace=%s" % color["space"])
    return "setparams=" + ":".join(parts) if parts else ""


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return run_capture(cmd)


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

    # 2) container info: audio presence, size, rotation, colour
    info = _run([ffmpeg, "-nostdin", "-hide_banner", "-i", path]).stderr or ""
    has_audio = bool(re.search(r"Stream #\d+:\d+.*?: Audio:", info))
    v = parse_video_info(info)
    w, h = v["width"], v["height"]
    if v["rotation"] in (90, 270):
        w, h = h, w            # ffmpeg rotates while decoding: frames come out upright
    return Probe(n, fps, duration, uniform, regular, drift, has_audio, w, h,
                 v["rotation"], v["pix_fmt"], v["color"])


def expected_frames(probe: Probe, plan: Plan) -> int:
    """Number of frames the decoder produces for ``plan`` (used for progress)."""
    if plan.action in ("retime", "copy") or not plan.target:
        return max(1, probe.frames)
    return max(1, int(round(probe.duration * plan.target)))


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
# settings of each output (the manifest)
# --------------------------------------------------------------------------

def output_signature(plan: Plan, variant: str, repair, flagged: Optional[Iterable[int]],
                     preset: str, crf: int) -> dict:
    """What an output was made with. Two runs with the same signature give the same file.

    ``repair`` is a ``dedupe.RepairOptions`` or None. When the held frames are known (the app
    passes its analysis or the manual edits), their list is part of the signature.
    """
    sig = {"format": 1, "variant": variant or "off", "action": plan.action,
           "target": plan.target, "atempo": round(float(plan.atempo or 1.0), 6),
           "preset": preset, "crf": int(crf)}
    if repair is not None:
        sig["mode"] = repair.mode
        sig["audio"] = repair.audio if repair.mode == "cut" else ""
        if flagged is not None:
            text = ",".join(str(int(f)) for f in sorted(set(flagged)))
            sig["held"] = hashlib.sha1(text.encode("ascii")).hexdigest()[:16]
        else:
            sig["held"] = "auto:%s" % repair.sensitivity
    return sig


def _manifest_key(job: Job) -> str:
    try:
        return job.dst.relative_to(job.out_root).as_posix()
    except ValueError:
        return job.dst.name


def read_manifest(out_root: Path) -> dict:
    try:
        with open(out_root / MANIFEST_NAME, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_manifest(out_root: Path, data: dict) -> None:
    tmp = out_root / (MANIFEST_NAME + ".tmp")
    try:
        out_root.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, out_root / MANIFEST_NAME)
    except OSError:
        _silent_remove(tmp)


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


def part_path(dst: Path) -> Path:
    """Temporary name used while an output is written (never left behind)."""
    return dst.with_name(dst.stem + ".part" + dst.suffix)


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
        self._manifests: Dict[Path, dict] = {}

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

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # single steps --------------------------------------------------------
    def probe(self, path: Union[str, Path]) -> Probe:
        return probe_file(self.ffmpeg, path)

    def plan(self, probe: Probe, mode: Union[str, int]) -> Plan:
        return make_plan(probe, mode, self.tolerance)

    def _manifest(self, out_root: Path) -> dict:
        if out_root not in self._manifests:
            self._manifests[out_root] = read_manifest(out_root)
        return self._manifests[out_root]

    def _remember(self, job: Job, sig: dict) -> None:
        data = self._manifest(job.out_root)
        data[_manifest_key(job)] = sig
        write_manifest(job.out_root, data)

    def process(self, job: Job, mode: Union[str, int],
                on_progress: Optional[Callable[[float], None]] = None,
                overwrite: bool = False,
                flagged: Optional[List[int]] = None) -> JobResult:
        """Probe, plan and convert one job. Errors are returned, not raised."""
        try:
            if self._cancel.is_set():
                return JobResult(job, "cancelled")
            probe = self.probe(job.src)
            plan = self.plan(probe, mode)
            res = JobResult(job, "done", plan.reason, probe.fps, plan.target, plan.action)
            if plan.action == "skip":
                res.status = "skipped"
                return res
            sig = output_signature(plan, job.variant, self.repair, flagged, self.preset, self.crf)
            if (self.skip_existing and not overwrite
                    and job.dst.exists() and job.dst.stat().st_size > 0):
                known = self._manifest(job.out_root).get(_manifest_key(job))
                if known is None:
                    return JobResult(job, "exists", "already in recoded folder (settings unknown)",
                                     probe.fps, plan.target, plan.action)
                if known == sig:
                    return JobResult(job, "exists", "already in recoded folder, same settings",
                                     probe.fps, plan.target, plan.action)
                res.message = "%s (settings changed, redone)" % res.message
            job.dst.parent.mkdir(parents=True, exist_ok=True)
            if self.repair is not None:
                from . import dedupe

                if flagged is None:
                    flagged, _ = dedupe.analyse(self.ffmpeg, job.src, probe, plan,
                                                self.repair.sensitivity, self._cancel)
                res.dups = len(flagged)
                if flagged:
                    self._repair(job, probe, plan, res, flagged, on_progress)
                    self._remember(job, sig)
                    return res
            if plan.action == "copy":
                self._copy(job)
                res.status = "copied"
                self._remember(job, sig)
                return res
            self._encode(job, probe, plan, on_progress)
            self._remember(job, sig)
            return res
        except Cancelled:
            return JobResult(job, "cancelled")
        except Exception as exc:  # noqa: BLE001
            return JobResult(job, "error", str(exc))

    def _copy(self, job: Job) -> None:
        tmp = part_path(job.dst)
        try:
            shutil.copy2(job.src, tmp)
            os.replace(tmp, job.dst)
        except BaseException:
            _silent_remove(tmp)
            raise

    def _repair(self, job: Job, probe: Probe, plan: Plan, res: JobResult,
                flagged: List[int],
                on_progress: Optional[Callable[[float], None]]) -> None:
        from . import dedupe

        tmp = part_path(job.dst)
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
        tmp = part_path(job.dst)
        expected = (probe.frames / plan.target) if plan.action == "retime" else probe.duration
        cmd = build_command(self.ffmpeg, job.src, tmp, plan, self.preset, self.crf)
        errors: List[str] = []
        proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        self._proc = proc
        ok = False
        try:
            if self._cancel.is_set():          # cancelled between two steps
                proc.terminate()
            assert proc.stdout is not None
            for line in proc.stdout:
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
            rc = proc.wait()
            if self._cancel.is_set():
                raise Cancelled()
            if rc != 0:
                raise RuntimeError(errors[-1] if errors else "ffmpeg failed (code %d)" % rc)
            self._verify(tmp, probe, plan)      # the result is checked BEFORE it replaces anything
            os.replace(tmp, job.dst)
            ok = True
        finally:
            self._proc = None
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            release(proc)
            if not ok:
                _silent_remove(tmp)
        if on_progress:
            on_progress(100.0)

    def _verify(self, path: Path, probe: Probe, plan: Plan) -> None:
        out = self.probe(path)
        if not out.uniform or abs(out.fps - plan.target) / plan.target > 1e-4:  # type: ignore[operator]
            raise RuntimeError("output is not a clean %s fps file" % plan.target)
        if plan.action == "retime" and abs(out.frames - probe.frames) > 1:
            raise RuntimeError("frame count changed during retime")

    # batch ---------------------------------------------------------------
    def run(self, jobs: List[Job], mode: Union[str, int],
            on_event: Optional[EventCallback] = None,
            overwrite: Optional[Iterable[Union[str, Path]]] = None,
            flags: Optional[dict] = None,
            variants: Optional[List[tuple]] = None,
            use_suffix: bool = False,
            reset: bool = True) -> List[JobResult]:
        """Process jobs in order.

        ``variants`` is a list of (key, RepairOptions or None): each clip is written once per
        variant (see ``variant_job`` for the file names). Without it, the single output uses
        ``self.repair``. Entries of ``overwrite`` are redone even if an output exists: either a
        source path (all variants) or "<source path>|<variant key>".

        ``reset`` clears an earlier cancellation. A caller that starts ``run`` in another
        thread resets first and passes ``reset=False``, so that a Cancel pressed before the
        thread starts is not lost.
        """
        if reset:
            self.reset()
        self._manifests = {}
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
    by_root: Dict[Path, List[JobResult]] = {}
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
