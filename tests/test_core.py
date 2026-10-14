"""Core tests. They use a real ffmpeg binary and tiny synthetic clips."""

import subprocess
from pathlib import Path

import pytest

from framesnap import core
from framesnap.core import (
    Recoder, collect_jobs, make_plan, nearest_standard, probe_file, Probe,
)

FFMPEG = core.find_ffmpeg()


def _gen(path: Path, vf_extra: str = "", rate: str = "24", seconds: float = 3.0,
         audio: bool = True, enc_extra=None) -> Path:
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
           "testsrc2=size=320x180:rate=%s" % rate]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    cmd += ["-t", str(seconds)]
    if vf_extra:
        cmd += ["-vf", vf_extra]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if enc_extra:
        cmd += enc_extra
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("clips")
    c = {}
    c["ntsc24"] = _gen(d / "ntsc24.mp4", rate="24000/1001")
    c["ntsc30"] = _gen(d / "ntsc30.mp4", rate="30000/1001")
    c["clean24"] = _gen(d / "clean24.mp4", rate="24",
                        enc_extra=["-video_track_timescale", "24000"])
    c["fps25"] = _gen(d / "fps25.mp4", rate="25")
    c["fps60"] = _gen(d / "fps60.mp4", rate="60")
    c["fps50"] = _gen(d / "fps50.mp4", rate="50")
    c["noaudio"] = _gen(d / "noaudio.mp4", rate="24000/1001", audio=False)
    # 24 fps content stamped on a 60 Hz grid (3 then 2 ticks): the Dreamina case
    c["pulldown"] = _gen(
        d / "pulldown.mp4", rate="24",
        vf_extra="settb=1/15360,setpts='(floor(N/2)*5+mod(N,2)*3)/(60*TB)'",
        enc_extra=["-fps_mode", "passthrough", "-video_track_timescale", "15360"],
    )
    # genuinely irregular timestamps around 24 fps
    c["vfr"] = _gen(
        d / "vfr.mp4", rate="24",
        vf_extra="settb=1/15360,setpts='(N/24+0.05*sin(N*0.9))/TB'",
        enc_extra=["-fps_mode", "passthrough", "-video_track_timescale", "15360"],
    )
    return c


# ---------------------------------------------------------------- helpers

def test_nearest_standard():
    assert nearest_standard(23.6) == 24
    assert nearest_standard(24.003) == 24
    assert nearest_standard(29.97) == 30
    assert nearest_standard(29.4) == 30
    assert nearest_standard(59.94) == 60
    assert nearest_standard(25.1) == 25


def _p(fps, uniform=True, regular=True, audio=True):
    return Probe(100, fps, 100 / fps, uniform, regular, 0.0, audio)


def test_plan_auto_snaps_near_values():
    assert make_plan(_p(23.6, uniform=False), "auto").target == 24
    assert make_plan(_p(24.003, uniform=False), "auto").target == 24
    assert make_plan(_p(29.4, uniform=False), "auto").target == 30
    assert make_plan(_p(59.94, uniform=False), "auto").target == 60


def test_plan_auto_skips_far_values():
    for fps in (12, 15, 48, 50):
        assert make_plan(_p(fps), "auto").action == "skip"


def test_plan_manual_converts_far_values():
    plan = make_plan(_p(50), 24)
    assert plan.action == "resample" and plan.target == 24


def test_plan_copy_when_already_clean():
    assert make_plan(_p(24.0), "auto").action == "copy"
    assert make_plan(_p(24.0), 24).action == "copy"


def test_plan_atempo_only_when_it_matters():
    p = make_plan(_p(24000 / 1001, uniform=True), 24)
    assert p.action == "retime" and abs(p.atempo - 1.001) < 1e-4
    p = make_plan(_p(24.0001, uniform=False), 24)
    assert p.atempo == 1.0
    p = make_plan(_p(23.93, uniform=False), 24)
    assert p.action == "retime" and p.atempo == 1.0
    p = make_plan(_p(24000 / 1001, uniform=True, audio=False), 24)
    assert p.atempo == 1.0


# ---------------------------------------------------------------- probing

def test_probe_ntsc(clips):
    p = probe_file(FFMPEG, clips["ntsc24"])
    assert abs(p.fps - 23.976) < 0.01 and p.uniform and p.has_audio


def test_probe_pulldown_is_24_but_not_uniform(clips):
    p = probe_file(FFMPEG, clips["pulldown"])
    assert abs(p.fps - 24.0) < 0.2
    assert not p.uniform and p.regular


def test_probe_vfr_is_irregular(clips):
    p = probe_file(FFMPEG, clips["vfr"])
    assert not p.regular


def test_probe_no_audio(clips):
    assert not probe_file(FFMPEG, clips["noaudio"]).has_audio


# ------------------------------------------------------------- collection

def test_collect_jobs_keeps_names_and_structure(tmp_path, clips):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "recoded").mkdir()
    (tmp_path / "a" / "one.mp4").write_bytes(clips["fps25"].read_bytes())
    (tmp_path / "a" / "b" / "two.MOV").write_bytes(b"x")
    (tmp_path / "a" / "._junk.mp4").write_bytes(b"x")
    (tmp_path / "a" / "note.txt").write_text("x")
    (tmp_path / "recoded" / "old.mp4").write_bytes(b"x")
    jobs = collect_jobs([tmp_path])
    rels = sorted(j.rel.replace("\\", "/") for j in jobs)
    assert rels == ["a/b/two.MOV", "a/one.mp4"]
    for j in jobs:
        assert j.dst == tmp_path / "recoded" / j.rel
    single = collect_jobs([tmp_path / "a" / "one.mp4"])
    assert single[0].dst == tmp_path / "a" / "recoded" / "one.mp4"


# -------------------------------------------------------------- recoding

def _recode(tmp_path, clip, mode, **kw):
    src = tmp_path / clip.name
    src.write_bytes(clip.read_bytes())
    rec = Recoder(FFMPEG, preset="ultrafast", crf=18, **kw)
    jobs = collect_jobs([src])
    return rec.run(jobs, mode)[0], jobs[0]


def test_ntsc_24_snaps_to_24_without_losing_frames(tmp_path, clips):
    src_p = probe_file(FFMPEG, clips["ntsc24"])
    res, job = _recode(tmp_path, clips["ntsc24"], "auto")
    assert res.status == "done" and res.target == 24 and res.action == "retime"
    out = probe_file(FFMPEG, job.dst)
    assert out.uniform and abs(out.fps - 24) < 1e-6
    assert out.frames == src_p.frames and out.has_audio
    assert job.dst.name == clips["ntsc24"].name


def test_ntsc_30_snaps_to_30(tmp_path, clips):
    res, job = _recode(tmp_path, clips["ntsc30"], "auto")
    assert res.target == 30
    out = probe_file(FFMPEG, job.dst)
    assert out.uniform and abs(out.fps - 30) < 1e-6


def test_pulldown_becomes_clean_24(tmp_path, clips):
    src_p = probe_file(FFMPEG, clips["pulldown"])
    res, job = _recode(tmp_path, clips["pulldown"], "auto")
    assert res.status == "done" and res.action == "retime"
    out = probe_file(FFMPEG, job.dst)
    assert out.uniform and abs(out.fps - 24) < 1e-6 and out.frames == src_p.frames


def test_vfr_is_resampled_keeping_duration(tmp_path, clips):
    src_p = probe_file(FFMPEG, clips["vfr"])
    res, job = _recode(tmp_path, clips["vfr"], "auto")
    assert res.action == "resample"
    out = probe_file(FFMPEG, job.dst)
    assert out.uniform and abs(out.fps - 24) < 1e-6
    assert abs(out.duration - src_p.duration) < 0.15


def test_auto_skips_50fps_and_logs_it(tmp_path, clips):
    res, job = _recode(tmp_path, clips["fps50"], "auto")
    assert res.status == "skipped" and not job.dst.exists()
    log = (job.out_root / core.LOG_NAME).read_text()
    assert "skipped" in log and "fps50.mp4" in log


def test_manual_target_converts_50_to_25_keeping_duration(tmp_path, clips):
    src_p = probe_file(FFMPEG, clips["fps50"])
    res, job = _recode(tmp_path, clips["fps50"], 25)
    assert res.status == "done" and res.action == "resample"
    out = probe_file(FFMPEG, job.dst)
    assert out.uniform and abs(out.fps - 25) < 1e-6
    assert abs(out.duration - src_p.duration) < 0.1


def test_manual_target_60_from_24(tmp_path, clips):
    res, job = _recode(tmp_path, clips["clean24"], 60)
    out = probe_file(FFMPEG, job.dst)
    assert out.uniform and abs(out.fps - 60) < 1e-6


def test_clean_clip_is_copied_byte_for_byte(tmp_path, clips):
    res, job = _recode(tmp_path, clips["clean24"], "auto")
    assert res.status == "copied"
    assert job.dst.read_bytes() == job.src.read_bytes()


def test_no_audio_clip_works(tmp_path, clips):
    res, job = _recode(tmp_path, clips["noaudio"], "auto")
    assert res.status == "done"
    assert not probe_file(FFMPEG, job.dst).has_audio


def test_existing_output_is_skipped_then_overwritten_on_demand(tmp_path, clips):
    res, job = _recode(tmp_path, clips["fps25"], "auto")
    assert res.status == "copied" or res.status == "done"
    rec = Recoder(FFMPEG, preset="ultrafast", skip_existing=True)
    assert rec.run([job], 30)[0].status == "exists"
    rec = Recoder(FFMPEG, preset="ultrafast", skip_existing=False)
    assert rec.run([job], 30)[0].status == "done"
    assert abs(probe_file(FFMPEG, job.dst).fps - 30) < 1e-6


def test_unreadable_file_reports_error(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    rec = Recoder(FFMPEG, preset="ultrafast")
    res = rec.run(collect_jobs([bad]), "auto")[0]
    assert res.status == "error" and res.message


def test_cancel_stops_batch(tmp_path, clips):
    for i in range(3):
        (tmp_path / ("c%d.mp4" % i)).write_bytes(clips["ntsc24"].read_bytes())
    rec = Recoder(FFMPEG, preset="ultrafast")
    jobs = collect_jobs([tmp_path])
    seen = []

    def on_event(job, kind, info):
        seen.append(kind)
        if kind == "result":
            rec.cancel()

    results = rec.run(jobs, "auto", on_event)
    assert results[0].status == "done"
    assert [r.status for r in results[1:]] == ["cancelled", "cancelled"]
    assert not list(tmp_path.rglob("*.part.*"))


# ------------------------------------------------------- platform helpers

def test_bundle_roots_cover_mac_and_windows_layouts(monkeypatch, tmp_path):
    exe = tmp_path / "FrameSnap.app" / "Contents" / "MacOS" / "FrameSnap"
    monkeypatch.setattr(core.sys, "frozen", True, raising=False)
    monkeypatch.setattr(core.sys, "executable", str(exe))
    monkeypatch.setattr(core.sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    roots = core.bundle_roots()
    assert str(tmp_path / "meipass") in roots
    assert str(tmp_path / "FrameSnap.app" / "Contents" / "Resources") in roots
    assert str(exe.parent / "_internal") in roots


def test_find_ffmpeg_prefers_bundled_copy(monkeypatch, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    exe = fake / core.FFMPEG_EXE
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(core.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv("FRAMESNAP_FFMPEG", raising=False)
    assert core.find_ffmpeg() == str(exe)


def test_subprocess_kwargs_hides_console_only_on_windows(monkeypatch):
    monkeypatch.setattr(core, "IS_WINDOWS", False)
    assert core.subprocess_kwargs() == {}
    monkeypatch.setattr(core, "IS_WINDOWS", True)
    monkeypatch.setattr(core.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert core.subprocess_kwargs() == {"creationflags": 0x08000000}
