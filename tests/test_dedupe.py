"""Held frame detection and repair, on synthetic clips with known held frames."""

import os
import subprocess

import numpy as np
import pytest

from framesnap import core, dedupe, rife
from framesnap.core import Recoder, collect_jobs, make_plan, probe_file
from framesnap.dedupe import (
    Interpolator, RepairOptions, analyse, detect_duplicates, frame_diffs, group_runs, repair,
)

FFMPEG = core.find_ffmpeg()
W, H, FPS, N = 320, 180, 24, 48
HELD = [5, 11, 17, 23, 29, 35, 41]          # frames that repeat their predecessor


def _texture(width, height, seed=3):
    rng = np.random.default_rng(seed)
    import cv2

    base = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
    return cv2.GaussianBlur(base, (0, 0), 2.5)


def _frames(held=(), cut_at=None):
    wide = _texture(W + 8 * N, H)
    other = np.full((H, W + 8 * N, 3), 235, dtype=np.uint8)      # a very different shot
    rng = np.random.default_rng(1)
    frames = []
    for i in range(N):
        src = other if (cut_at is not None and i >= cut_at) else wide
        f = src[:, i * 8:i * 8 + W].copy()
        if i in held:
            noise = rng.integers(-1, 2, f.shape)
            f = np.clip(frames[-1].astype(np.int16) + noise, 0, 255).astype(np.uint8)
        frames.append(f)
    return frames


def _write(path, frames, audio=True):
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", "%dx%d" % (W, H), "-r", str(FPS), "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-t", "%.4f" % (len(frames) / FPS)]
    cmd += [str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(f.tobytes())
    p.stdin.close()
    assert p.wait() == 0
    return path


@pytest.fixture(scope="module")
def held_clip(tmp_path_factory):
    return _write(tmp_path_factory.mktemp("held") / "held.mp4", _frames(HELD))


@pytest.fixture(scope="module")
def clean_clip(tmp_path_factory):
    return _write(tmp_path_factory.mktemp("clean") / "clean.mp4", _frames(()))


def _prep(path):
    probe = probe_file(FFMPEG, path)
    return probe, make_plan(probe, "auto")


def _opts(**kw):
    kw.setdefault("engine", "cpu")
    return RepairOptions(**kw)


def _decode(path):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-fps_mode", "passthrough",
                        "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8)
    return a.reshape(-1, H, W, 3)


def _audio_stats(path):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                        "-ar", "48000", "-f", "s16le", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float64)
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a))))
    return len(a) / 48000.0, float(np.fft.rfftfreq(len(a), 1 / 48000.0)[spec.argmax()])


# ---------------------------------------------------------------- detection

def test_detects_exactly_the_held_frames(held_clip):
    probe, plan = _prep(held_clip)
    flagged, diffs = analyse(FFMPEG, held_clip, probe, plan)
    assert flagged == HELD
    assert len(diffs) == N - 1


def test_no_false_positive_on_clean_motion(clean_clip):
    probe, plan = _prep(clean_clip)
    assert analyse(FFMPEG, clean_clip, probe, plan)[0] == []


def test_still_shot_is_not_flagged():
    assert detect_duplicates([0.01] * 40) == []


def test_sensitivity_levels():
    d = [5.0] * 20
    d[10] = 1.0                       # 20 percent of the local motion
    assert detect_duplicates(d, "strict") == []
    assert detect_duplicates(d, "normal") == [11]
    d[10] = 1.8                       # 36 percent
    assert detect_duplicates(d, "normal") == []
    assert detect_duplicates(d, "loose") == [11]


def test_runs_are_grouped():
    assert group_runs([3, 4, 9, 12, 13, 14]) == [(3, 4), (9, 9), (12, 14)]


def test_exact_zero_difference_is_found():
    d = [4.0] * 20
    d[7] = 0.0
    assert detect_duplicates(d) == [8]


# ------------------------------------------------------------ mode A

def test_interpolate_keeps_length_and_removes_held_frames(tmp_path, held_clip):
    probe, plan = _prep(held_clip)
    out = tmp_path / "a.mp4"
    rep = repair(FFMPEG, held_clip, out, probe, plan, _opts(mode="interpolate"), "ultrafast", 14)
    assert rep.frames_in == rep.frames_out == N and rep.repaired == len(HELD)
    assert "optical flow CPU" in rep.backend
    p2 = probe_file(FFMPEG, out)
    assert p2.frames == N and p2.uniform and abs(p2.fps - 24) < 1e-6
    assert analyse(FFMPEG, out, p2, make_plan(p2, "auto"))[0] == []
    # the audio is untouched: same duration, same pitch
    dur_in, pk_in = _audio_stats(held_clip)
    dur_out, pk_out = _audio_stats(out)
    assert abs(dur_in - dur_out) < 0.06 and abs(pk_in - pk_out) < 3


def test_interpolated_frame_is_closer_to_the_truth_than_the_copy(tmp_path, held_clip):
    probe, plan = _prep(held_clip)
    out = tmp_path / "a.mp4"
    repair(FFMPEG, held_clip, out, probe, plan, _opts(mode="interpolate"), "ultrafast", 8)
    fixed = _decode(out).astype(np.float32)
    truth = _frames(())                                  # the same shot without any held frame
    held = _frames(HELD)
    for i in (5, 11, 23):
        err_fixed = np.abs(fixed[i] - truth[i].astype(np.float32)).mean()
        err_copy = np.abs(held[i].astype(np.float32) - truth[i].astype(np.float32)).mean()
        # clearly better than the copy; the exact ratio varies a little between platforms
        # (about 0.45 on Linux, 0.55 on Apple Silicon), so the margin is wide
        assert err_fixed < 0.75 * err_copy, (i, err_fixed, err_copy)


def test_scene_cut_next_to_a_held_frame_is_left_alone(tmp_path):
    frames = _frames(held=(24,), cut_at=24)           # held frame right after a hard cut
    clip = _write(tmp_path / "cut.mp4", frames)
    probe, plan = _prep(clip)
    out = tmp_path / "o.mp4"
    rep = repair(FFMPEG, clip, out, probe, plan, _opts(mode="interpolate"), "ultrafast", 14,
                 flagged=[24])
    assert rep.skipped_cuts == 1 and rep.repaired == 0
    assert probe_file(FFMPEG, out).frames == N


# ------------------------------------------------------------ mode B

def test_cut_keep_audio_pads_with_green_screen(tmp_path, held_clip):
    probe, plan = _prep(held_clip)
    out = tmp_path / "b1.mp4"
    rep = repair(FFMPEG, held_clip, out, probe, plan, _opts(mode="cut", audio="keep"), "ultrafast", 14)
    kept = N - len(HELD)
    assert rep.frames_out == kept and rep.placeholder_frames >= len(HELD) - 1
    p2 = probe_file(FFMPEG, out)
    assert p2.frames == kept + rep.placeholder_frames and p2.uniform
    dur_audio, _ = _audio_stats(out)
    assert p2.duration >= dur_audio - 0.05            # the picture lasts as long as the sound
    frames = _decode(out)
    last = frames[-1].astype(int)
    assert last[..., 1].mean() > 150 and last[..., 0].mean() < 60   # green (BGR)
    assert frames[kept - 1][..., 1].mean() < 140                    # real picture before it


def test_cut_stretch_audio_matches_new_length_and_keeps_pitch(tmp_path, held_clip):
    probe, plan = _prep(held_clip)
    out = tmp_path / "b2.mp4"
    rep = repair(FFMPEG, held_clip, out, probe, plan, _opts(mode="cut", audio="stretch"), "ultrafast", 14)
    kept = N - len(HELD)
    assert rep.frames_out == kept and rep.placeholder_frames == 0
    assert abs(rep.audio_tempo - N / kept) < 1e-6
    p2 = probe_file(FFMPEG, out)
    assert p2.frames == kept and p2.uniform
    dur, pitch = _audio_stats(out)
    assert abs(dur - kept / FPS) < 0.08
    assert abs(pitch - 440) < 4                       # pitch preserved


def test_atempo_chain_handles_any_factor():
    assert dedupe.atempo_chain(1.25) == "atempo=1.250000"
    assert dedupe.atempo_chain(3.0).count("atempo") == 2
    assert dedupe.atempo_chain(0.3).count("atempo") == 2


def test_placeholder_text_fits_vertical_and_horizontal_frames():
    for w, h in ((1280, 720), (720, 1280), (320, 180)):
        img = dedupe.placeholder_frame(w, h)
        assert img.shape == (h, w, 3)
        white = (img.min(axis=2) > 200)
        assert white.any()
        cols = np.where(white.any(axis=0))[0]
        assert cols.min() >= 0 and cols.max() < w


# ------------------------------------------------------- through Recoder

def test_recoder_repairs_and_logs(tmp_path, held_clip):
    src = tmp_path / "clip.mp4"
    src.write_bytes(held_clip.read_bytes())
    rec = Recoder(FFMPEG, preset="ultrafast", crf=14, repair=_opts(mode="interpolate"))
    job = collect_jobs([src])[0]
    res = rec.run([job], "auto")[0]
    assert res.status == "done" and res.dups == len(HELD) and res.repair == "interpolate"
    assert job.dst.name == "clip.mp4"
    assert "held frames" in (job.out_root / core.LOG_NAME).read_text()


def test_recoder_without_held_frames_behaves_like_v1(tmp_path, clean_clip):
    src = tmp_path / "clip.mp4"
    src.write_bytes(clean_clip.read_bytes())
    rec = Recoder(FFMPEG, preset="ultrafast", crf=14, repair=_opts(mode="cut", audio="stretch"))
    res = rec.run(collect_jobs([src]), "auto")[0]
    assert res.dups == 0 and res.repair == "" and res.status in ("done", "copied")


def test_recoder_skip_plan_does_not_run_repair(tmp_path):
    clip = tmp_path / "fifty.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=50", "-t", "2", "-c:v", "libx264",
                    "-preset", "ultrafast", str(clip)], check=True)
    rec = Recoder(FFMPEG, preset="ultrafast", repair=_opts(mode="interpolate"))
    assert rec.run(collect_jobs([clip]), "auto")[0].status == "skipped"


def test_recoder_cancel_during_repair_leaves_no_partial_file(tmp_path, held_clip):
    src = tmp_path / "clip.mp4"
    src.write_bytes(held_clip.read_bytes())
    rec = Recoder(FFMPEG, preset="ultrafast", repair=_opts(mode="interpolate"))
    jobs = collect_jobs([src])

    def on_event(job, kind, info):
        if kind == "progress":
            rec.cancel()

    res = rec.run(jobs, "auto", on_event)[0]
    assert res.status == "cancelled"
    assert not list(tmp_path.rglob("*.part.*")) and not jobs[0].dst.exists()


# ------------------------------------------------------------ engines

def test_forced_gpu_falls_back_to_cpu_when_rife_is_missing(monkeypatch):
    monkeypatch.setattr(rife, "find_rife", lambda: None)
    monkeypatch.setattr(rife, "_check_cache", {})
    it = Interpolator("gpu")
    a = _frames()[0]
    b = _frames()[2]
    out = it.between(a, b, [0.5])
    assert out[0].shape == a.shape
    assert it.backends_used == ["optical flow CPU"] and it.fallback_reason


def test_cpu_engine_never_touches_rife(monkeypatch):
    monkeypatch.setattr(rife, "find_rife", lambda: (_ for _ in ()).throw(AssertionError("rife was used")))
    it = Interpolator("cpu")
    it.between(_frames()[0], _frames()[2], [0.25, 0.75])
    assert it.backends_used == ["optical flow CPU"] and it.fallback_reason == ""


def test_auto_prefers_cpu_flow_when_vulkan_is_software(monkeypatch):
    monkeypatch.setattr(rife, "_check_cache", {"r": {"available": True, "ok": True,
                        "device": "llvmpipe (LLVM 20)", "software": True, "seconds": 1,
                        "error": ""}})
    it = Interpolator("auto")
    it.between(_frames()[0], _frames()[2], [0.5])
    assert it.backends_used == ["optical flow CPU"]


def test_software_device_names_are_recognised():
    assert rife.is_software_device("llvmpipe (LLVM 20.1.2, 256 bits)")
    assert rife.is_software_device("SwiftShader Device")
    assert not rife.is_software_device("Apple M2 Pro")
    assert not rife.is_software_device("NVIDIA GeForce RTX 4070")


needs_rife = pytest.mark.skipif(
    rife.find_rife() is None or not rife.gpu_check()["ok"],
    reason="rife-ncnn-vulkan or a Vulkan device (even software) is not available")


@needs_rife
def test_rife_midpoint_is_close_to_the_truth():
    frames = _frames()
    a, truth, b = frames[10], frames[11], frames[12]
    mid = rife.Rife().interpolate(a, b, [0.5])[0]
    assert mid.shape == a.shape
    err_rife = np.abs(mid.astype(np.float32) - truth.astype(np.float32)).mean()
    err_copy = np.abs(a.astype(np.float32) - truth.astype(np.float32)).mean()
    assert err_rife < err_copy


@needs_rife
def test_forced_gpu_engine_end_to_end_with_rife(tmp_path, held_clip):
    probe, plan = _prep(held_clip)
    out = tmp_path / "gpu.mp4"
    rep = repair(FFMPEG, held_clip, out, probe, plan,
                 RepairOptions(mode="interpolate", engine="gpu"), "ultrafast", 14,
                 flagged=HELD[:2])
    assert rep.backend.startswith("RIFE GPU") and rep.repaired == 2
    assert probe_file(FFMPEG, out).frames == N


@needs_rife
def test_gpu_check_reports_device():
    r = rife.gpu_check(force=True)
    assert r["ok"] and r["device"]


def test_forced_gpu_falls_back_when_rife_crashes(monkeypatch, tmp_path):
    if os.name == "nt":
        fake = tmp_path / "rife-fail.bat"
        fake.write_text("@echo off\r\nexit /b 3\r\n")
    else:
        fake = tmp_path / "rife-fail.sh"
        fake.write_text("#!/bin/sh\nexit 3\n")
        fake.chmod(0o755)
    (tmp_path / rife.MODEL_NAME).mkdir()
    (tmp_path / rife.MODEL_NAME / "flownet.param").write_text("x")
    monkeypatch.setattr(rife, "find_rife", lambda: fake)
    it = Interpolator("gpu")
    out = it.between(_frames()[0], _frames()[2], [0.5, 0.75])
    assert len(out) == 2 and it.backends_used == ["optical flow CPU"]
    assert "rife" in it.fallback_reason.lower() and it.fallback_reason


def test_gpu_check_survives_a_rife_that_cannot_start(monkeypatch, tmp_path):
    exe = tmp_path / rife.RIFE_EXE
    exe.write_bytes(b"not an executable")
    exe.chmod(0o755)
    (tmp_path / rife.MODEL_NAME).mkdir()
    (tmp_path / rife.MODEL_NAME / "flownet.param").write_text("x")
    monkeypatch.setattr(rife, "find_rife", lambda: exe)
    r = rife.gpu_check(force=True)
    assert r["available"] and not r["ok"] and r["error"]
    monkeypatch.undo()
    rife._check_cache.clear()


def test_missing_vulkan_driver_is_reported_without_starting_rife(monkeypatch, tmp_path):
    fake = tmp_path / rife.RIFE_EXE
    fake.write_text("never started")
    (tmp_path / rife.MODEL_NAME).mkdir()
    monkeypatch.setattr(rife, "find_rife", lambda: fake)
    monkeypatch.setattr(rife, "vulkan_runtime_present", lambda: False)
    monkeypatch.setattr(rife, "_check_cache", {})
    r = rife.gpu_check(force=True)
    assert r["available"] and not r["ok"] and "Vulkan" in r["error"]
    assert not rife.gpu_usable()
    with pytest.raises(rife.RifeError, match="Vulkan"):
        rife.Rife(fake)._run(fake, fake, tmp_path / "o.png", 0.5)
    it = Interpolator("gpu")
    assert len(it.between(_frames()[0], _frames()[2], [0.5])) == 1
    assert it.backends_used == ["optical flow CPU"] and "Vulkan" in it.fallback_reason
