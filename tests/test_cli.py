"""Command line interface."""

import pytest

from framesnap import cli, core, rife

from .test_core import FFMPEG, clips  # noqa: F401
from .test_dedupe import HELD, N, held_clip  # noqa: F401


def _held(tmp_path, held_clip):
    clip = tmp_path / "held.mp4"
    clip.write_bytes(held_clip.read_bytes())
    return clip


def _run(*args):
    return cli.main([str(a) for a in args])


def test_several_modes_go_to_one_folder_each(tmp_path, held_clip, capsys):
    clip = _held(tmp_path, held_clip)
    rc = _run(clip, "--repair", "interpolate,cut-keep", "--repair", "cut-stretch",
              "--engine", "cpu", "--preset", "ultrafast")
    assert rc == 0
    for folder in ("inter", "keep", "speed"):
        assert (tmp_path / "recoded" / folder / "held.mp4").exists()
    assert not (tmp_path / "recoded" / "cadence").exists()
    out = capsys.readouterr().out
    assert "recoded/inter/held.mp4" in out.replace("\\", "/")


def test_suffix_option(tmp_path, held_clip):
    clip = _held(tmp_path, held_clip)
    assert _run(clip, "--repair", "off", "--repair", "interpolate", "--suffix",
                "--engine", "cpu", "--preset", "ultrafast") == 0
    names = sorted(p.name for p in (tmp_path / "recoded").glob("*.mp4"))
    assert names == ["held_recode-inter.mp4", "held_recode.mp4"]


def test_default_is_still_rate_only_with_the_same_name(tmp_path, held_clip):
    clip = _held(tmp_path, held_clip)
    assert _run(clip, "--preset", "ultrafast") == 0
    assert (tmp_path / "recoded" / "held.mp4").exists()


def test_unknown_mode_is_refused(tmp_path, held_clip, capsys):
    clip = _held(tmp_path, held_clip)
    with pytest.raises(SystemExit):
        _run(clip, "--repair", "bogus")
    assert "unknown --repair mode" in capsys.readouterr().err


def test_dry_run_counts_held_frames(tmp_path, held_clip, capsys):
    clip = _held(tmp_path, held_clip)
    assert _run(clip, "--dry-run", "--repair", "interpolate") == 0
    assert "%d held frames" % len(HELD) in capsys.readouterr().out
    assert not (tmp_path / "recoded").exists()


def test_gpu_check_reports_and_never_crashes(capsys):
    rc = _run("--gpu-check")
    out = capsys.readouterr().out
    assert "rife-ncnn-vulkan:" in out and "GPU interpolation will be" in out
    assert rc in (0, 1)


def test_paths_are_required_without_gpu_check(capsys):
    with pytest.raises(SystemExit):
        cli.main([])
    assert "paths" in capsys.readouterr().err
