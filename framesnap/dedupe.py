"""Detect and repair duplicated (held) frames.

AI video generators often hold a frame: the picture is repeated (or almost:
compression noise makes the copy differ by a fraction of a level), which reads
as stutter or stop motion. This module

1. detects those frames by comparing each frame with its predecessor, relative
   to the local amount of motion (an absolute threshold would miss near copies
   and would flag still scenes),
2. repairs them in one of two ways:

   * ``interpolate``: every held frame is replaced by a motion compensated
     frame computed from its real neighbours (RIFE on the GPU, or dense optical
     flow, DIS from OpenCV). Frame count and duration are untouched, so the
     audio is untouched.
   * ``cut``: held frames are removed. The video gets shorter. The audio is
     either kept as is (``keep``: the picture ends first and a placeholder
     screen is shown until the sound ends) or time stretched with ``atempo``
     to the new length (``stretch``: pitch is preserved).

Colour fidelity: frames travel between the two ffmpeg processes as 8 bit
YUV 4:2:0, exactly as the encoder wants them, so the frames that are kept are
never converted. Only the frames that are computed go through RGB (the RGB
round trip of OpenCV is unbiased to about 0.1 level), and the colour tags of the
source (range, matrix, primaries, transfer) are written to the output.

Frames are streamed through ffmpeg pipes, so memory use stays small.
"""

from __future__ import annotations

import math
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple, Union

import numpy as np

from .core import (
    Cancelled, Plan, Probe, StderrTail, color_args, output_color, popen, release,
    run_capture, setparams_filter,
)

DETECT_WIDTH = 192
SENSITIVITY = {"strict": 0.15, "normal": 0.25, "loose": 0.40}
MIN_LOCAL_MOTION = 0.4        # gray levels: below this the shot is static, nothing to fix
ABS_FLOOR = 0.02
SCENE_CUT_LEVEL = 25.0        # mean gray difference above which two frames are different shots
FLOW_MAX_SIDE = 1080
PLACEHOLDER_TEXT = "VIDEO SHORTER THAN SOUNDTRACK"


class RepairError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def _detect_size(width: int, height: int) -> Tuple[int, int]:
    sw = DETECT_WIDTH
    sh = max(2, int(round(height * sw / max(1, width) / 2.0)) * 2)
    return sw, sh


def decode_filter(plan: Plan) -> str:
    """Filter that turns the source into frames at the target rate (as v1 does)."""
    if plan.action == "retime":
        return "setpts=N/(%d*TB)" % int(plan.target)  # type: ignore[arg-type]
    if plan.action == "resample":
        return "fps=%d" % int(plan.target)  # type: ignore[arg-type]
    return ""


def frame_diffs(ffmpeg: str, src: Union[str, Path], vf: str, width: int, height: int,
                cancel: Optional[threading.Event] = None,
                on_progress: Optional[Callable[[int], None]] = None) -> np.ndarray:
    """Mean absolute gray difference between each frame and the previous one."""
    sw, sh = _detect_size(width, height)
    chain = ",".join(x for x in (vf, "scale=%d:%d" % (sw, sh), "format=gray") if x)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(src),
           "-map", "0:v:0", "-vf", chain, "-fps_mode", "passthrough",
           "-f", "rawvideo", "-"]
    proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tail = StderrTail(proc.stderr)
    size = sw * sh
    diffs: List[float] = []
    prev = None
    finished = False
    try:
        assert proc.stdout is not None
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            cur = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
            if prev is not None:
                diffs.append(float(np.abs(cur - prev).mean()))
                if on_progress is not None and len(diffs) % 24 == 0:
                    on_progress(len(diffs) + 1)
            prev = cur
        finished = True
    finally:
        if proc.poll() is None and not finished:
            proc.kill()
        rc = proc.wait()
        tail.join()
        release(proc)
    if rc != 0 and not diffs:
        raise RepairError("decoding failed: %s" % tail.last("ffmpeg code %d" % rc))
    return np.asarray(diffs, dtype=np.float64)


def detect_duplicates(diffs: Sequence[float], sensitivity: str = "normal",
                      window: int = 4) -> List[int]:
    """Indices of frames that repeat their predecessor.

    ``diffs[i]`` compares frame i+1 with frame i, so a small ``diffs[i]`` means
    frame i+1 is a copy and the returned index is i+1. A difference counts as small
    relative to the median of its neighbours (``window`` on each side, itself
    excluded). Vectorised: an hour at 60 fps takes a fraction of a second.
    """
    ratio = SENSITIVITY[sensitivity]
    d = np.asarray(diffs, dtype=np.float64)
    n = len(d)
    if n < 2:
        return []
    from numpy.lib.stride_tricks import sliding_window_view

    padded = np.concatenate([np.full(window, np.nan), d, np.full(window, np.nan)])
    win = sliding_window_view(padded, 2 * window + 1).copy()
    win[:, window] = np.nan                          # the difference itself is not a neighbour
    valid = ~np.isnan(win)
    counts = valid.sum(axis=1)
    # median of the valid neighbours: sort with NaN last, average the middle pair
    srt = np.sort(np.where(valid, win, np.inf), axis=1)
    lo_i = np.clip((counts - 1) // 2, 0, None)
    hi_i = np.clip(counts // 2, 0, None)
    rows = np.arange(n)
    local = (srt[rows, lo_i] + srt[rows, hi_i]) / 2.0
    hit = (counts > 0) & (local > MIN_LOCAL_MOTION) & (d < np.maximum(ABS_FLOOR, ratio * local))
    return [int(i) + 1 for i in np.nonzero(hit)[0]]


def group_runs(flagged: Sequence[int]) -> List[Tuple[int, int]]:
    """Consecutive flagged frames as inclusive (first, last) runs."""
    runs: List[Tuple[int, int]] = []
    for f in sorted(flagged):
        if runs and f == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], f)
        else:
            runs.append((f, f))
    return runs


def run_counts(flagged: Sequence[int]) -> dict:
    """How many frames are shown twice ("double"), three times ("triple") or more."""
    counts = {"double": 0, "triple": 0, "more": 0}
    for first, last in group_runs(flagged):
        length = last - first + 1            # extra copies of the same picture
        counts["double" if length == 1 else "triple" if length == 2 else "more"] += 1
    return counts


def timeline_data(diffs: Sequence[float], flagged: Sequence[int], fps: float,
                  max_points: int = 600) -> dict:
    """Everything the timeline needs: the motion curve (down sampled) and the held runs.

    ``motion[k]`` is the mean difference of the frames in bin k (``bin`` frames per bin,
    starting at frame 1). ``runs`` is a list of [first_frame, extra_copies].
    """
    d = np.asarray(diffs, dtype=np.float64)
    n = len(d) + 1
    size = max(1, int(math.ceil(len(d) / float(max_points)))) if len(d) else 1
    if len(d):
        pad = (-len(d)) % size
        padded = np.concatenate([d, np.full(pad, np.nan)]) if pad else d
        binned = np.nanmean(padded.reshape(-1, size), axis=1)
    else:
        binned = np.asarray([])
    return {
        "n": n, "fps": float(fps), "seconds": n / float(fps) if fps else 0.0,
        "bin": size, "motion": [round(float(x), 2) for x in binned],
        "peak": round(float(binned.max()), 2) if len(binned) else 0.0,
        "runs": [[first, last - first + 1] for first, last in group_runs(flagged)],
        "counts": run_counts(flagged),
    }


# --------------------------------------------------------------------------
# optical flow interpolation
# --------------------------------------------------------------------------

_dis_local = threading.local()


def _dis():
    import cv2

    if not hasattr(_dis_local, "dis"):
        _dis_local.dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    return _dis_local.dis


def _flow(a_gray: np.ndarray, b_gray: np.ndarray) -> np.ndarray:
    return _dis().calc(a_gray, b_gray, None)


def _warp(img: np.ndarray, flow: np.ndarray) -> np.ndarray:
    import cv2

    h, w = flow.shape[:2]
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    return cv2.remap(img, gx + flow[..., 0], gy + flow[..., 1],
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


class Interpolator:
    """Computes frames between two real frames A and B for several t in (0, 1).

    engine: "auto" uses RIFE on the GPU when a real GPU works, otherwise the CPU
    optical flow; "gpu" asks for RIFE (still falls back if it fails); "cpu" forces
    the CPU optical flow.
    """

    def __init__(self, engine: str = "auto", flow_max_side: int = FLOW_MAX_SIDE):
        self.flow_max_side = flow_max_side
        self.engine = engine
        self.backends_used: List[str] = []
        self.fallback_reason = ""
        self._rife = None
        if engine in ("auto", "gpu"):
            try:
                from . import rife as _rife_mod

                if engine == "gpu" or _rife_mod.gpu_usable():
                    self._rife = _rife_mod.Rife()
            except Exception as exc:  # noqa: BLE001
                self.fallback_reason = str(exc)
                self._rife = None

    def _note(self, name: str) -> None:
        if name not in self.backends_used:
            self.backends_used.append(name)

    def between(self, a: np.ndarray, b: np.ndarray, ts: Sequence[float]) -> List[np.ndarray]:
        if self._rife is not None:
            try:
                out = self._rife.interpolate(a, b, ts)
                self._note("RIFE GPU" + (" (%s)" % self._rife.device if self._rife.device else ""))
                return out
            except Exception as exc:  # noqa: BLE001
                self.fallback_reason = str(exc)
                self._rife = None  # stop trying for the rest of the clip
        self._note("optical flow CPU")
        return self._dis_between(a, b, ts)

    def _dis_between(self, a: np.ndarray, b: np.ndarray, ts: Sequence[float]) -> List[np.ndarray]:
        import cv2

        h, w = a.shape[:2]
        scale = min(1.0, self.flow_max_side / float(max(h, w)))
        if scale < 1.0:
            sa = cv2.resize(a, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            sb = cv2.resize(b, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        else:
            sa, sb = a, b
        ga, gb = cv2.cvtColor(sa, cv2.COLOR_BGR2GRAY), cv2.cvtColor(sb, cv2.COLOR_BGR2GRAY)
        fab, fba = _flow(ga, gb), _flow(gb, ga)
        if scale < 1.0:
            fab = cv2.resize(fab, (w, h), interpolation=cv2.INTER_LINEAR) / scale
            fba = cv2.resize(fba, (w, h), interpolation=cv2.INTER_LINEAR) / scale
        out = []
        for t in ts:
            wa = _warp(a, t * fba)
            wb = _warp(b, (1.0 - t) * fab)
            out.append(cv2.addWeighted(wa, 1.0 - t, wb, t, 0))
        return out


# --------------------------------------------------------------------------
# audio helpers
# --------------------------------------------------------------------------

def atempo_chain(factor: float) -> str:
    """ffmpeg atempo filter for any factor (each atempo handles 0.5 to 2)."""
    parts: List[str] = []
    f = factor
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append("atempo=%.6f" % f)
    return ",".join(parts)


# --------------------------------------------------------------------------
# placeholder screen
# --------------------------------------------------------------------------

def placeholder_frame(width: int, height: int, text: str = PLACEHOLDER_TEXT) -> np.ndarray:
    import cv2

    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:] = (0, 200, 0)  # green (BGR)
    font = cv2.FONT_HERSHEY_DUPLEX
    scale = 1.0
    (tw, th), _ = cv2.getTextSize(text, font, scale, 2)
    scale = scale * (width * 0.8) / max(1, tw)
    thick = max(2, int(round(scale * 2)))
    (tw, th), base = cv2.getTextSize(text, font, scale, thick)
    if tw > width * 0.9:  # narrow vertical frames: split in two lines
        words = text.split(" ")
        mid = len(words) // 2
        lines = [" ".join(words[:mid]), " ".join(words[mid:])]
        scale = scale * 0.55
        thick = max(2, int(round(scale * 2)))
        sizes = [cv2.getTextSize(l, font, scale, thick)[0] for l in lines]
        total = sum(s[1] for s in sizes) + int(0.6 * sizes[0][1])
        y = (height - total) // 2 + sizes[0][1]
        for l, s in zip(lines, sizes):
            cv2.putText(img, l, ((width - s[0]) // 2, y), font, scale, (255, 255, 255), thick, cv2.LINE_AA)
            y += int(1.6 * s[1])
    else:
        cv2.putText(img, text, ((width - tw) // 2, (height + th) // 2), font, scale,
                    (255, 255, 255), thick, cv2.LINE_AA)
    return img


def scene_change(a_y: np.ndarray, b_y: np.ndarray) -> float:
    """Mean difference of two luma planes, on the 0..255 gray scale, at low resolution."""
    import cv2

    h, w = a_y.shape[:2]
    sw = 192
    sh = max(2, int(h * sw / max(1, w)))
    ga = cv2.resize(a_y, (sw, sh), interpolation=cv2.INTER_AREA).astype(np.float32)
    gb = cv2.resize(b_y, (sw, sh), interpolation=cv2.INTER_AREA).astype(np.float32)
    return float(np.abs(ga - gb).mean()) * 255.0 / 219.0     # limited range luma to gray levels


def audio_duration(ffmpeg: str, src: Union[str, Path]) -> float:
    """Duration in seconds of the first audio stream (0 if none)."""
    r = run_capture(
        [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(src),
         "-map", "0:a:0", "-c", "copy", "-f", "null", "-", "-progress", "pipe:1", "-nostats"])
    if r.returncode != 0:
        return 0.0
    vals = re.findall(r"out_time_us=(\d+)", r.stdout)
    return int(vals[-1]) / 1e6 if vals else 0.0


# --------------------------------------------------------------------------
# 8 bit YUV 4:2:0 frames (I420), as ffmpeg's rawvideo yuv420p
# --------------------------------------------------------------------------

def i420_size(width: int, height: int) -> int:
    return width * height * 3 // 2


def i420_to_bgr(buf: bytes, width: int, height: int) -> np.ndarray:
    import cv2

    yuv = np.frombuffer(buf, dtype=np.uint8).reshape(height * 3 // 2, width)
    return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)


def bgr_to_i420(img: np.ndarray) -> bytes:
    import cv2

    return cv2.cvtColor(img, cv2.COLOR_BGR2YUV_I420).tobytes()


def luma(buf: bytes, width: int, height: int) -> np.ndarray:
    return np.frombuffer(buf, dtype=np.uint8, count=width * height).reshape(height, width)


# --------------------------------------------------------------------------
# the repair itself
# --------------------------------------------------------------------------

@dataclass
class RepairOptions:
    mode: str = "interpolate"        # interpolate | cut
    audio: str = "stretch"           # cut only: keep | stretch
    sensitivity: str = "normal"      # strict | normal | loose
    engine: str = "auto"             # auto | gpu | cpu (interpolate mode)
    message: str = PLACEHOLDER_TEXT


@dataclass
class RepairReport:
    frames_in: int = 0
    frames_out: int = 0
    flagged: List[int] = field(default_factory=list)
    repaired: int = 0
    skipped_cuts: int = 0
    seconds_in: float = 0.0
    seconds_out: float = 0.0
    audio_tempo: float = 1.0
    placeholder_frames: int = 0
    backend: str = ""
    fallback_reason: str = ""

    @property
    def percent(self) -> float:
        return 100.0 * len(self.flagged) / self.frames_in if self.frames_in else 0.0


def analyse(ffmpeg: str, src: Union[str, Path], probe: Probe, plan: Plan,
            sensitivity: str = "normal",
            cancel: Optional[threading.Event] = None) -> Tuple[List[int], np.ndarray]:
    diffs = frame_diffs(ffmpeg, src, decode_filter(plan), probe.width, probe.height, cancel)
    return detect_duplicates(diffs, sensitivity), diffs


def _encoder_cmd(ffmpeg: str, src: Path, dst: Path, w: int, h: int, fps: int,
                 audio_filter: str, has_audio: bool, preset: str, crf: int,
                 color: Optional[dict] = None) -> List[str]:
    color = color or {}
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "%dx%d" % (w, h), "-r", str(fps),
           "-i", "-", "-i", str(src), "-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "1:a?"]
    tags = setparams_filter(color)
    if tags:
        cmd += ["-vf", tags]          # tag the raw frames so that nothing converts them
    cmd += ["-r", str(fps), "-fps_mode", "cfr",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-x264-params", "keyint=%d:min-keyint=%d" % (fps, fps)]
    cmd += color_args(color)
    cmd += ["-video_track_timescale", str(fps * 1000), "-map_metadata", "1",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000"]
    if audio_filter:
        cmd += ["-af", audio_filter]
    cmd += ["-movflags", "+faststart", str(dst)]
    return cmd


def repair(ffmpeg: str, src: Union[str, Path], dst: Union[str, Path], probe: Probe, plan: Plan,
           options: RepairOptions, preset: str = "slow", crf: int = 12,
           on_progress: Optional[Callable[[float], None]] = None,
           cancel: Optional[threading.Event] = None,
           flagged: Optional[Sequence[int]] = None) -> RepairReport:
    """Write ``dst``: the clip at the target rate with held frames repaired."""
    if plan.action == "skip" or not plan.target:
        raise RepairError("no target frame rate for this clip")
    src, dst = Path(src), Path(dst)
    fps = int(plan.target)
    w, h = probe.width, probe.height
    if w <= 0 or h <= 0:
        raise RepairError("unknown frame size")
    if w % 2 or h % 2:
        raise RepairError("frame size %dx%d is odd: H.264 in 4:2:0 needs even sizes" % (w, h))
    frames_in = probe.frames if plan.action in ("retime", "copy") else int(round(probe.duration * fps))
    if flagged is None:
        flagged, diffs = analyse(ffmpeg, src, probe, plan, options.sensitivity, cancel)
        frames_in = len(diffs) + 1
    flagged = sorted(set(int(f) for f in flagged))
    flagged_set = set(flagged)
    report = RepairReport(frames_in=frames_in, flagged=list(flagged))
    report.seconds_in = frames_in / float(fps)
    n_out = frames_in - (len(flagged) if options.mode == "cut" else 0)
    report.frames_out = n_out
    report.seconds_out = n_out / float(fps)

    # audio plan
    base_tempo = plan.atempo if plan.atempo else 1.0
    audio_tempo = base_tempo
    pad_frames = 0
    if probe.has_audio and options.mode == "cut" and flagged:
        if options.audio == "stretch":
            audio_tempo = base_tempo * (report.seconds_in / report.seconds_out)
        else:  # keep: the picture ends first, fill the rest with a placeholder
            audio_len = audio_duration(ffmpeg, src) / base_tempo
            extra = audio_len - report.seconds_out
            if extra > 1.0 / fps:
                pad_frames = int(math.ceil(extra * fps))
    report.audio_tempo = audio_tempo
    report.placeholder_frames = pad_frames
    audio_filter = atempo_chain(audio_tempo) if abs(audio_tempo - 1.0) > 2e-4 else ""

    # decoder: same frames as the detection pass, full resolution, 8 bit YUV 4:2:0
    vf = decode_filter(plan)
    dec = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(src), "-map", "0:v:0"]
    if vf:
        dec += ["-vf", vf]
    dec += ["-fps_mode", "passthrough", "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"]
    enc_cmd = _encoder_cmd(ffmpeg, src, dst, w, h, fps, audio_filter, probe.has_audio, preset, crf,
                           output_color(probe))
    decoder = popen(dec, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dec_err = StderrTail(decoder.stderr)
    encoder = popen(enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    enc_err = StderrTail(encoder.stderr)
    interp = Interpolator(options.engine)
    frame_bytes = i420_size(w, h)

    def put(frame: bytes) -> None:
        try:
            encoder.stdin.write(frame)  # type: ignore[union-attr]
        except (BrokenPipeError, OSError):
            enc_err.join(1)
            raise RepairError(enc_err.last("encoder stopped"))

    def fix_run(prev: bytes, nxt: bytes, count: int) -> List[bytes]:
        if scene_change(luma(prev, w, h), luma(nxt, w, h)) > SCENE_CUT_LEVEL:
            report.skipped_cuts += count
            return [prev] * count
        ts = [(j + 1) / float(count + 1) for j in range(count)]
        report.repaired += count
        made = interp.between(i420_to_bgr(prev, w, h), i420_to_bgr(nxt, w, h), ts)
        return [bgr_to_i420(f) for f in made]

    ok = False
    try:
        assert decoder.stdout is not None
        prev: Optional[bytes] = None
        pending = 0
        i = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            buf = decoder.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            if i in flagged_set:
                if options.mode == "interpolate" and prev is not None:
                    pending += 1
                # cut mode: drop the frame
            else:
                if pending and prev is not None:
                    for f in fix_run(prev, buf, pending):
                        put(f)
                    pending = 0
                put(buf)
                prev = buf
            i += 1
            if on_progress and frames_in:
                on_progress(min(99.0, 100.0 * i / frames_in))
        if decoder.wait() != 0:
            dec_err.join(1)
            raise RepairError("decoding failed: %s" % dec_err.last("ffmpeg code %d" % decoder.returncode))
        if pending and prev is not None:  # held frames at the very end: nothing after to interpolate to
            for _ in range(pending):
                put(prev)
        if pad_frames:
            pad = bgr_to_i420(placeholder_frame(w, h, options.message))
            for _ in range(pad_frames):
                put(pad)
        encoder.stdin.close()  # type: ignore[union-attr]
        rc = encoder.wait()
        enc_err.join(5)
        if rc != 0:
            raise RepairError(enc_err.last("ffmpeg failed (code %d)" % rc))
        ok = True
        report.backend = " + ".join(interp.backends_used)
        report.fallback_reason = interp.fallback_reason
    finally:
        for p in (decoder, encoder):
            if p.poll() is None:
                p.kill()
            p.wait()
            release(p)
        if not ok:
            try:
                dst.unlink()
            except OSError:
                pass
    if on_progress:
        on_progress(100.0)
    return report
