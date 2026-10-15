"""Colour fidelity of the repaired outputs, rotation, and the colour tags.

Up to 2.2.x every frame of a repaired output went through RGB and back, which made the
picture darker and tinted (about -2 to -3 levels), held frames or not. These tests fail
on that engine.
"""
import subprocess

import numpy as np
import pytest

from framesnap import core, dedupe
from framesnap.core import Recoder, collect_jobs, make_plan, parse_video_info, probe_file
from framesnap.dedupe import RepairOptions, repair

FFMPEG = core.find_ffmpeg()
W, H, FPS, N = 320, 180, 24, 48
HELD = [6, 13, 20, 27, 34]


def _colour_frames(held=()):
    """Saturated, moving colour content (a plain gray picture would hide a colour shift)."""
    rng = np.random.default_rng(7)
    import cv2

    wide = cv2.GaussianBlur(rng.integers(0, 255, (H, W + 6 * N, 3), dtype=np.uint8), (0, 0), 3)
    bars = np.zeros_like(wide)
    for k, col in enumerate([(40, 40, 220), (40, 200, 40), (220, 60, 40), (30, 200, 230)]):
        bars[:, k * (bars.shape[1] // 4):(k + 1) * (bars.shape[1] // 4)] = col
    src = cv2.addWeighted(wide, 0.5, bars, 0.5, 0)
    frames = []
    for i in range(N):
        f = src[:, i * 6:i * 6 + W].copy()
        if i in held:
            f = frames[-1].copy()
        frames.append(f)
    return frames


def _write(path, frames, extra=()):
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", "%dx%d" % (W, H), "-r", str(FPS), "-i", "-",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8"] + list(extra) + [
           "-c:a", "aac", "-t", "%.4f" % (len(frames) / FPS), str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(f.tobytes())
    p.stdin.close()
    assert p.wait() == 0
    return path


BT709 = ["-pix_fmt", "yuv420p", "-vf", "scale=out_color_matrix=bt709:out_range=tv",
         "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]


@pytest.fixture(scope="module")
def tagged_clip(tmp_path_factory):
    return _write(tmp_path_factory.mktemp("colour") / "bt709.mp4", _colour_frames(HELD), BT709)


def _yuv(path, w=W, h=H):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-fps_mode", "passthrough",
                        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, h * 3 // 2, w).astype(np.float64)


def _plane_bias(a, b, w=W, h=H):
    """Mean signed difference of the Y, U and V planes."""
    d = a - b
    return (d[:, :h].mean(), d[:, h:h + h // 4].mean(), d[:, h + h // 4:].mean())


@pytest.mark.parametrize("mode,audio", [("interpolate", "stretch"), ("cut", "keep"), ("cut", "stretch")])
def test_frames_that_are_kept_keep_their_colours(tmp_path, tagged_clip, mode, audio):
    probe = probe_file(FFMPEG, tagged_clip)
    plan = make_plan(probe, "auto")
    out = tmp_path / "out.mp4"
    repair(FFMPEG, tagged_clip, out, probe, plan, RepairOptions(mode, audio, engine="cpu"),
           "ultrafast", 8, flagged=HELD)
    src, dst = _yuv(tagged_clip), _yuv(out)
    kept = [i for i in range(N) if i not in HELD]
    if mode == "interpolate":
        a, b = dst[kept], src[kept]
    else:
        a, b = dst[:len(kept)], src[kept]
    bias = _plane_bias(a, b)
    assert all(abs(x) < 0.3 for x in bias), bias          # 2.2.x: about -2 on Y


def test_repaired_output_matches_the_rate_only_output(tmp_path, tagged_clip):
    src = tmp_path / "clip.mp4"
    src.write_bytes(tagged_clip.read_bytes())
    rec = Recoder(FFMPEG, preset="ultrafast", crf=8)
    rec.run(collect_jobs([src]), "auto",
            variants=[("off", None), ("interpolate", RepairOptions("interpolate", engine="cpu"))])
    rate, inter = src.parent / "recoded" / "cadence" / "clip.mp4", src.parent / "recoded" / "inter" / "clip.mp4"
    kept = [i for i in range(N) if i not in HELD]

    def bgr(path):
        r = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo",
                            "-pix_fmt", "bgr24", "-"], capture_output=True, check=True)
        return np.frombuffer(r.stdout, np.uint8).reshape(-1, H, W, 3).astype(np.float64)

    a, b = bgr(inter)[kept], bgr(rate)[kept]
    shift = [(a[..., c] - b[..., c]).mean() for c in range(3)]
    assert all(abs(s) < 0.5 for s in shift), shift


def test_colour_tags_of_the_source_are_written(tmp_path, tagged_clip):
    probe = probe_file(FFMPEG, tagged_clip)
    assert probe.color == {"range": "tv", "space": "bt709", "primaries": "bt709", "trc": "bt709"}
    out = tmp_path / "out.mp4"
    repair(FFMPEG, tagged_clip, out, probe, make_plan(probe, "auto"),
           RepairOptions("interpolate", engine="cpu"), "ultrafast", 14, flagged=HELD)
    assert probe_file(FFMPEG, out).color == probe.color


def test_full_range_source_is_written_like_the_rate_only_output(tmp_path):
    clip = _write(tmp_path / "pc.mp4", _colour_frames(HELD), ["-pix_fmt", "yuvj420p"])
    probe = probe_file(FFMPEG, clip)
    assert probe.pix_fmt == "yuvj420p" and probe.color.get("range") == "pc"
    rec = Recoder(FFMPEG, preset="ultrafast", crf=8)
    rec.run(collect_jobs([clip]), "auto",
            variants=[("off", None), ("cut_stretch", RepairOptions("cut", "stretch", engine="cpu"))])
    rate = _yuv(tmp_path / "recoded" / "cadence" / "pc.mp4")
    speed = _yuv(tmp_path / "recoded" / "speed" / "pc.mp4")
    kept = [i for i in range(N) if i not in HELD]
    bias = _plane_bias(speed[:len(kept)], rate[kept])
    assert all(abs(x) < 0.3 for x in bias), bias
    # converted to limited range like the rate only output: never tagged full range any more
    assert probe_file(FFMPEG, tmp_path / "recoded" / "speed" / "pc.mp4").color.get("range") != "pc"


def test_rgb_round_trip_of_computed_frames_is_unbiased(tagged_clip):
    yuv = _yuv(tagged_clip)[0].astype(np.uint8)
    back = np.frombuffer(dedupe.bgr_to_i420(dedupe.i420_to_bgr(yuv.tobytes(), W, H)), np.uint8)
    d = back.reshape(yuv.shape).astype(np.float64) - yuv
    assert abs(d.mean()) < 0.15 and np.abs(d).mean() < 0.5


# ------------------------------------------------------------------ rotation

def test_rotated_phone_clip_is_repaired_upright(tmp_path, tagged_clip):
    rotated = tmp_path / "phone.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-display_rotation", "90", "-i", str(tagged_clip),
                    "-c", "copy", str(rotated)], check=True)
    probe = probe_file(FFMPEG, rotated)
    assert probe.rotation in (90, 270) and (probe.width, probe.height) == (H, W)
    rec = Recoder(FFMPEG, preset="ultrafast", crf=8)
    rec.run(collect_jobs([rotated]), "auto",
            variants=[("off", None), ("interpolate", RepairOptions("interpolate", engine="cpu"))])
    rate = _yuv(tmp_path / "recoded" / "cadence" / "phone.mp4", H, W)
    inter = _yuv(tmp_path / "recoded" / "inter" / "phone.mp4", H, W)
    kept = [i for i in range(N) if i not in HELD]
    err = np.abs(inter[kept][:, :W] - rate[kept][:, :W]).mean()
    assert err < 2.0, err                              # 2.2.x: scrambled picture, error above 40


# ------------------------------------------------------------------ parsing and limits

def test_video_info_parsing():
    line = ("  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), "
            "yuv420p(tv, bt709, progressive), 1470x630 [SAR 1:1 DAR 7:3], 24 fps\n")
    v = parse_video_info(line)
    assert (v["width"], v["height"], v["pix_fmt"]) == (1470, 630, "yuv420p")
    assert v["color"] == {"range": "tv", "space": "bt709", "primaries": "bt709", "trc": "bt709"}
    hdr = parse_video_info("Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084),"
                           " 3840x2160, 25 fps\n      displaymatrix: rotation of -90.00 degrees\n"
                           "  Stream #0:1: Audio: aac\n")
    assert hdr["color"] == {"range": "tv", "space": "bt2020nc", "primaries": "bt2020", "trc": "smpte2084"}
    assert hdr["rotation"] == 270
    plain = parse_video_info("Stream #0:0: Video: h264 (High), yuv420p(progressive), 640x360, 24 fps\n")
    assert plain["color"] == {} and plain["pix_fmt"] == "yuv420p"
    odd = parse_video_info("Stream #0:0: Video: h264, yuv420p(tv, weird/unknown/x), 640x360\n")
    assert odd["color"] == {"range": "tv"}                 # unknown names are never passed to ffmpeg


def test_odd_frame_size_is_refused_with_a_clear_message(tmp_path, tagged_clip):
    probe = probe_file(FFMPEG, tagged_clip)
    probe.width = 319
    with pytest.raises(dedupe.RepairError, match="odd"):
        repair(FFMPEG, tagged_clip, tmp_path / "o.mp4", probe, make_plan(probe, "auto"),
               RepairOptions("interpolate", engine="cpu"), flagged=HELD)
