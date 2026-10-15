"""Processes, pipes, cancellation, atomic outputs, the settings manifest, detection speed."""
import json
import os
import sys
import threading
import time

import numpy as np
import pytest

from framesnap import core, dedupe
from framesnap.core import Recoder, collect_jobs, expected_frames, make_plan, probe_file
from framesnap.dedupe import RepairOptions

from .test_core import FFMPEG
from .test_dedupe import HELD, N

posix_only = pytest.mark.skipif(os.name == "nt", reason="fake ffmpeg written as a POSIX script")


def _fake_ffmpeg(tmp_path, body):
    """An executable that stands in for ffmpeg (Python script with a shebang)."""
    exe = tmp_path / "fake-ffmpeg"
    exe.write_text("#!%s\nimport sys\n%s\n" % (sys.executable, body))
    exe.chmod(0o755)
    return str(exe)


# ------------------------------------------------------------------ child processes

def test_kill_all_stops_registered_children():
    p = core.popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert p in core.live_processes()
    assert core.kill_all() >= 1
    assert p.poll() is not None and p not in core.live_processes()


@posix_only
def test_a_flood_of_decoder_errors_does_not_freeze_the_analysis(tmp_path):
    # 1 MB of error lines first (the pipe holds about 64 KB), then 30 gray frames of 192x108
    exe = _fake_ffmpeg(tmp_path, "\n".join([
        "sys.stderr.write('[h264 @ 0x1] error while decoding MB 12 34\\n' * 25000)",
        "sys.stderr.flush()",
        "out = sys.stdout.buffer",
        "for i in range(30):",
        "    out.write(bytes([i * 5 % 256]) * (192 * 108))",
        "out.flush()",
    ]))
    result = {}
    t = threading.Thread(target=lambda: result.update(d=dedupe.frame_diffs(exe, "x.mp4", "", 192, 108)))
    t.start()
    t.join(20)
    assert not t.is_alive(), "frame_diffs is stuck on a full stderr pipe"
    assert len(result["d"]) == 29


@posix_only
def test_a_decoder_that_fails_is_reported(tmp_path):
    exe = _fake_ffmpeg(tmp_path, "sys.stderr.write('moov atom not found\\n'); sys.exit(1)")
    with pytest.raises(dedupe.RepairError, match="moov atom not found"):
        dedupe.frame_diffs(exe, "x.mp4", "", 192, 108)


def test_no_process_is_left_after_a_run(tmp_path, clips):
    src = tmp_path / clips["ntsc24"].name
    src.write_bytes(clips["ntsc24"].read_bytes())
    Recoder(FFMPEG, preset="ultrafast").run(collect_jobs([src]), "auto")
    assert core.live_processes() == []


# ------------------------------------------------------------------ atomic outputs

def test_a_result_that_fails_verification_never_replaces_a_good_file(tmp_path, clips, monkeypatch):
    src = tmp_path / clips["ntsc24"].name
    src.write_bytes(clips["ntsc24"].read_bytes())
    job = collect_jobs([src])[0]
    assert Recoder(FFMPEG, preset="ultrafast").run([job], "auto")[0].status == "done"
    good = job.dst.read_bytes()

    def bad_verify(self, path, probe, plan):
        raise RuntimeError("output is not a clean 24 fps file")

    monkeypatch.setattr(Recoder, "_verify", bad_verify)
    res = Recoder(FFMPEG, preset="ultrafast", skip_existing=False).run([job], "auto")[0]
    assert res.status == "error" and "clean" in res.message
    assert job.dst.read_bytes() == good                    # 2.2.x deleted the good file
    assert not list(tmp_path.rglob("*.part.*"))


def test_an_interrupted_copy_leaves_nothing_behind(tmp_path, clips, monkeypatch):
    src = tmp_path / clips["clean24"].name
    src.write_bytes(clips["clean24"].read_bytes())
    job = collect_jobs([src])[0]

    def broken_copy(a, b, *k, **kw):
        with open(b, "wb") as fh:
            fh.write(b"half a file")
        raise OSError("disk full")

    monkeypatch.setattr(core.shutil, "copy2", broken_copy)
    res = Recoder(FFMPEG, preset="ultrafast").run([job], "auto")[0]
    assert res.status == "error" and "disk full" in res.message
    assert not job.dst.exists() and not list(tmp_path.rglob("*.part.*"))
    # so the next run does the copy instead of skipping a truncated file
    monkeypatch.undo()
    assert Recoder(FFMPEG, preset="ultrafast").run([job], "auto")[0].status == "copied"


# ------------------------------------------------------------------ settings manifest

def _manifest(job):
    return json.loads((job.out_root / core.MANIFEST_NAME).read_text())


def test_existing_output_is_redone_when_the_settings_changed(tmp_path, held_clip):
    src = tmp_path / "clip.mp4"
    src.write_bytes(held_clip.read_bytes())
    job = collect_jobs([src])[0]

    def rec(sens):
        return Recoder(FFMPEG, preset="ultrafast", crf=14,
                       repair=RepairOptions("cut", "stretch", sensitivity=sens, engine="cpu"))

    first = rec("normal").run([job], "auto")[0]
    assert first.status == "done" and list(_manifest(job)) == ["clip.mp4"]
    again = rec("normal").run([job], "auto")[0]
    assert again.status == "exists" and "same settings" in again.message
    changed = rec("loose").run([job], "auto")[0]
    assert changed.status == "done" and "settings changed" in changed.message
    assert _manifest(job)["clip.mp4"]["held"] == "auto:loose"


def test_manual_held_frames_are_part_of_the_settings(tmp_path, held_clip):
    src = tmp_path / "clip.mp4"
    src.write_bytes(held_clip.read_bytes())
    job = collect_jobs([src])[0]
    rec = Recoder(FFMPEG, preset="ultrafast", crf=14,
                  repair=RepairOptions("cut", "stretch", engine="cpu"))
    assert rec.run([job], "auto", flags={str(src): HELD})[0].status == "done"
    assert rec.run([job], "auto", flags={str(src): HELD})[0].status == "exists"
    res = rec.run([job], "auto", flags={str(src): HELD[:3]})[0]
    assert res.status == "done"
    assert probe_file(FFMPEG, job.dst).frames == N - 3


def test_outputs_of_older_versions_are_kept(tmp_path, clips):
    src = tmp_path / clips["fps25"].name
    src.write_bytes(clips["fps25"].read_bytes())
    job = collect_jobs([src])[0]
    job.dst.parent.mkdir(parents=True)
    job.dst.write_bytes(b"made by 2.2")
    res = Recoder(FFMPEG, preset="ultrafast").run([job], 30)[0]
    assert res.status == "exists" and "settings unknown" in res.message
    assert job.dst.read_bytes() == b"made by 2.2"


# ------------------------------------------------------------------ cancellation

def test_cancel_pressed_before_the_worker_starts_is_not_lost(tmp_path, clips):
    from framesnap.app import Controller

    class LateRecoder(Recoder):
        def run(self, *a, **kw):
            time.sleep(0.4)             # the worker thread is slow to start
            return super().run(*a, **kw)

    for i in range(2):
        (tmp_path / ("c%d.mp4" % i)).write_bytes(clips["ntsc24"].read_bytes())
    ctl = Controller(lambda s: None, check_gpu=False,
                     recoder_factory=lambda **k: LateRecoder(FFMPEG, **k), preset="ultrafast")
    ctl.add_paths([str(tmp_path)])
    end = time.time() + 30
    while any(i["status"] == "probing" for i in ctl.state()["items"]) and time.time() < end:
        time.sleep(0.05)
    assert ctl.start()
    ctl.cancel()
    while ctl.state()["running"] and time.time() < end:
        time.sleep(0.05)
    statuses = [r["status"] for it in ctl.state()["items"] for r in it["results"]]
    assert statuses == ["cancelled", "cancelled"]          # 2.2.x encoded both clips
    assert not (tmp_path / "recoded" / "c0.mp4").exists()


# ------------------------------------------------------------------ detection

def _reference_detect(diffs, sensitivity="normal", window=4):
    """The loop of 2.2.x, kept as the specification of the vectorised version."""
    ratio = dedupe.SENSITIVITY[sensitivity]
    d = np.asarray(diffs, dtype=np.float64)
    n = len(d)
    out = []
    for i in range(n):
        lo, hi = max(0, i - window), min(n, i + window + 1)
        nb = np.concatenate([d[lo:i], d[i + 1:hi]])
        if len(nb) == 0:
            continue
        local = float(np.median(nb))
        if local > dedupe.MIN_LOCAL_MOTION and d[i] < max(dedupe.ABS_FLOOR, ratio * local):
            out.append(i + 1)
    return out


def test_vectorised_detection_gives_exactly_the_same_frames():
    rng = np.random.default_rng(11)
    for n in list(range(0, 12)) + [50, 333, 2000]:
        d = rng.gamma(2.0, 2.0, n)
        d[rng.random(n) < 0.08] = rng.random() * 0.3       # held frames
        d[rng.random(n) < 0.05] = 0.0
        for sens in ("strict", "normal", "loose"):
            assert dedupe.detect_duplicates(d, sens) == _reference_detect(d, sens), (n, sens)


def test_detection_of_an_hour_at_60_fps_is_fast():
    d = np.random.default_rng(3).gamma(2.0, 2.0, 60 * 60 * 60)
    t0 = time.perf_counter()
    dedupe.detect_duplicates(d)
    assert time.perf_counter() - t0 < 2.0                  # 2.2.x: about 10 s, under the GUI lock


def test_expected_frames_follow_the_plan(clips):
    p = probe_file(FFMPEG, clips["fps50"])
    assert expected_frames(p, make_plan(p, 25)) == round(p.duration * 25)
    q = probe_file(FFMPEG, clips["ntsc24"])
    assert expected_frames(q, make_plan(q, "auto")) == q.frames
