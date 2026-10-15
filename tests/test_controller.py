"""Controller tests: the whole app logic without any window."""

import json
import subprocess
import time


from framesnap import core, dedupe
from framesnap.app import Api, Controller
from framesnap.core import Recoder, probe_file

from .test_core import FFMPEG
from .test_dedupe import HELD, N


def make(tmp_path, **kw):
    states = []
    ctl = Controller(states.append, check_gpu=False,
                     recoder_factory=lambda **k: Recoder(FFMPEG, **k),
                     preset="ultrafast", crf=18, **kw)
    return ctl, states


def wait_for(cond, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def wait_probed(ctl):
    wait_for(lambda: all(i["status"] != "probing" for i in ctl.state()["items"]))


def wait_analysed(ctl):
    wait_for(lambda: all(i["analysis"] in ("done", "skip", "error", "off")
                         for i in ctl.state()["items"]))


def copy_in(tmp_path, clips, *names):
    out = []
    for n in names:
        p = tmp_path / clips[n].name
        p.write_bytes(clips[n].read_bytes())
        out.append(str(p))
    return out


def held_in(tmp_path, held_clip, name="held.mp4"):
    clip = tmp_path / name
    clip.write_bytes(held_clip.read_bytes())
    return str(clip)


def result_status(item, variant=""):
    for r in item["results"]:
        if r["variant"] == variant:
            return r["status"]
    return None


# --------------------------------------------------------------- basics

def test_add_probe_plan_run(tmp_path, clips):
    ctl, states = make(tmp_path)
    paths = copy_in(tmp_path, clips, "ntsc24", "ntsc30", "fps50")
    assert ctl.add_paths(paths) == 3
    wait_probed(ctl)
    items = {i["name"]: i for i in ctl.state()["items"]}
    assert items["ntsc24.mp4"]["target"] == 24 and items["ntsc24.mp4"]["action"] == "retime"
    assert items["ntsc30.mp4"]["target"] == 30
    assert items["fps50.mp4"]["action"] == "skip"
    assert ctl.start(wait=True)
    st = {i["name"]: result_status(i, "off") for i in ctl.state()["items"]}
    assert st == {"ntsc24.mp4": "done", "ntsc30.mp4": "done", "fps50.mp4": "skipped"}
    assert not ctl.state()["running"]
    assert abs(probe_file(FFMPEG, tmp_path / "recoded" / "ntsc24.mp4").fps - 24) < 1e-6
    assert any(s["running"] for s in states)


def test_recode_again_without_reimporting(tmp_path, clips):
    ctl, _ = make(tmp_path)
    ctl.add_paths(copy_in(tmp_path, clips, "fps50"))
    wait_probed(ctl)
    ctl.start(wait=True)
    item = ctl.state()["items"][0]
    assert item["status"] == "ready" and result_status(item, "off") == "skipped"
    ctl.set_mode(25)                                       # nothing is imported again
    assert ctl.state()["items"][0]["action"] == "resample"
    assert ctl.start(wait=True)
    assert result_status(ctl.state()["items"][0], "off") == "done"
    assert abs(probe_file(FFMPEG, tmp_path / "recoded" / "fps50.mp4").fps - 25) < 1e-6
    ctl.set_mode(30)                                       # again, same session
    assert ctl.start(wait=True)
    assert abs(probe_file(FFMPEG, tmp_path / "recoded" / "fps50.mp4").fps - 30) < 1e-6
    assert ctl.start(wait=True)                            # and again with identical settings
    assert result_status(ctl.state()["items"][0], "off") == "done"


def test_outputs_of_a_previous_session_are_skipped_when_asked(tmp_path, clips):
    ctl, _ = make(tmp_path)
    ctl.add_paths(copy_in(tmp_path, clips, "fps25"))
    wait_probed(ctl)
    ctl.start(wait=True)
    ctl2, _ = make(tmp_path)                               # new session, same folder
    ctl2.add_paths([str(tmp_path / "fps25.mp4")])
    wait_probed(ctl2)
    ctl2.start(wait=True)
    assert result_status(ctl2.state()["items"][0], "off") == "exists"
    ctl2.set_skip_existing(False)
    ctl2.start(wait=True)
    assert result_status(ctl2.state()["items"][0], "off") in ("done", "copied")


def test_folder_drop_and_duplicates(tmp_path, clips):
    ctl, _ = make(tmp_path)
    copy_in(tmp_path, clips, "ntsc24", "fps25")
    assert ctl.add_paths([str(tmp_path)]) == 2
    assert ctl.add_paths([str(tmp_path)]) == 0
    wait_probed(ctl)
    assert len(ctl.state()["items"]) == 2


def test_remove_and_clear(tmp_path, clips):
    ctl, _ = make(tmp_path)
    ctl.add_paths(copy_in(tmp_path, clips, "ntsc24", "fps25"))
    wait_probed(ctl)
    wait_analysed(ctl)
    first = ctl.state()["items"][0]["id"]
    ctl.remove(first)
    assert len(ctl.state()["items"]) == 1
    assert ctl.get_timeline(first) is None
    ctl.clear()
    assert ctl.state()["items"] == []


def test_unreadable_file_is_an_error_item(tmp_path):
    ctl, _ = make(tmp_path)
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"nope")
    ctl.add_paths([str(bad)])
    wait_probed(ctl)
    item = ctl.state()["items"][0]
    assert item["status"] == "error" and item["message"]
    assert not ctl.start()


def test_cancel_returns_items_to_ready(tmp_path, clips):
    ctl, _ = make(tmp_path)
    for i in range(3):
        (tmp_path / ("c%d.mp4" % i)).write_bytes(clips["ntsc24"].read_bytes())
    ctl.add_paths([str(tmp_path)])
    wait_probed(ctl)
    ctl.start()
    ctl.cancel()
    wait_for(lambda: not ctl.state()["running"])
    statuses = [i["status"] for i in ctl.state()["items"]]
    assert set(statuses) == {"ready"}                      # can simply be started again
    assert not list(tmp_path.rglob("*.part.*"))


def test_state_is_json_serialisable(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    wait_analysed(ctl)
    json.dumps(ctl.state())
    json.dumps(ctl.get_timeline(ctl.state()["items"][0]["id"]))


def test_api_pushes_to_window(tmp_path, clips):
    api = Api()
    sent = []

    class Win:
        def evaluate_js(self, code):
            sent.append(code)

    api._attach(Win())
    api._c._factory = lambda **kw: Recoder(FFMPEG, **kw)
    api._c._preset = "ultrafast"
    api.add_paths(copy_in(tmp_path, clips, "ntsc24"))
    wait_probed(api._c)
    assert sent and sent[-1].startswith("window.render({")
    for name in ("toggle_repair", "set_use_suffix", "set_auto_analyse", "analyse_now", "get_timeline"):
        assert callable(getattr(api, name))


def test_gpu_status_is_part_of_the_state():
    ctl = Controller(lambda s: None, check_gpu=True,
                     recoder_factory=lambda **kw: Recoder(FFMPEG, **kw))
    wait_for(lambda: ctl.state()["repair"]["gpu"].get("checked"), 30)
    gpu = ctl.state()["repair"]["gpu"]
    assert "ok" in gpu and "software" in gpu


# --------------------------------------------------- automatic analysis

def test_held_frames_are_analysed_on_import_without_any_repair_mode(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    wait_analysed(ctl)
    item = ctl.state()["items"][0]
    assert ctl.state()["repair"]["modes"] == ["off"]
    assert item["analysis"] == "done" and item["dups"] == len(HELD)
    assert abs(item["dup_pct"] - 100.0 * len(HELD) / N) < 0.2
    assert item["runs"] == {"double": len(HELD), "triple": 0, "more": 0}


def test_analysis_can_be_switched_off_and_started_by_hand(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.set_auto_analyse(False)
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    time.sleep(0.3)
    assert ctl.state()["items"][0]["analysis"] == "off" and ctl.state()["items"][0]["dups"] is None
    ctl.analyse_now()
    wait_analysed(ctl)
    assert ctl.state()["items"][0]["dups"] == len(HELD)


def test_sensitivity_recounts_instantly_from_stored_differences(tmp_path, held_clip, monkeypatch):
    ctl, _ = make(tmp_path)
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    wait_analysed(ctl)
    v0 = ctl.state()["items"][0]["tl_v"]
    monkeypatch.setattr(dedupe, "frame_diffs",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("decoded again")))
    ctl.set_sensitivity("strict")
    ctl.set_sensitivity("loose")
    item = ctl.state()["items"][0]
    assert item["tl_v"] > v0 and item["analysis"] == "done"
    assert ctl.state()["repair"]["sensitivity"] == "loose"


def test_timeline_data_lists_the_held_runs(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    wait_analysed(ctl)
    tl = ctl.get_timeline(ctl.state()["items"][0]["id"])
    assert tl["n"] == N and abs(tl["fps"] - 24) < 1e-6
    assert [r[0] for r in tl["runs"]] == HELD and all(r[1] == 1 for r in tl["runs"])
    assert len(tl["motion"]) == N - 1 and tl["peak"] > 0
    assert tl["counts"]["double"] == len(HELD)


def test_timeline_downsamples_long_clips():
    d = [3.0] * 5000
    flagged = [10, 11, 2000]
    tl = dedupe.timeline_data(d, flagged, 24.0, max_points=600)
    assert len(tl["motion"]) <= 600 and tl["bin"] >= 9
    assert tl["runs"] == [[10, 2], [2000, 1]]
    assert tl["counts"] == {"double": 1, "triple": 1, "more": 0}


def test_changing_the_rate_reanalyses_when_the_frames_change(tmp_path, clips):
    ctl, _ = make(tmp_path)
    ctl.add_paths(copy_in(tmp_path, clips, "fps50"))
    wait_probed(ctl)
    wait_analysed(ctl)
    assert ctl.state()["items"][0]["analysis"] == "skip"   # 50 fps: not close to a target in auto
    ctl.set_mode(25)
    wait_for(lambda: ctl.state()["items"][0]["analysis"] == "done")
    assert ctl.get_timeline(ctl.state()["items"][0]["id"])["fps"] == 25


# ---------------------------------------------------- outputs and suffixes

def test_toggle_repair_keeps_at_least_one_output_in_canonical_order(tmp_path):
    ctl, _ = make(tmp_path)
    assert ctl.state()["repair"]["modes"] == ["off"]
    ctl.toggle_repair("off")                               # the last one cannot be removed
    assert ctl.state()["repair"]["modes"] == ["off"]
    ctl.toggle_repair("cut_stretch")
    ctl.toggle_repair("interpolate")
    assert ctl.state()["repair"]["modes"] == ["off", "interpolate", "cut_stretch"]
    ctl.toggle_repair("off")
    assert ctl.state()["repair"]["modes"] == ["interpolate", "cut_stretch"]
    ctl.toggle_repair("nonsense")
    ctl.set_repair_modes([])
    assert ctl.state()["repair"]["modes"] == ["interpolate", "cut_stretch"]


def test_single_mode_without_suffix_keeps_the_same_name(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.set_engine("cpu")
    ctl.set_repair("interpolate")
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    ctl.start(wait=True)
    assert (tmp_path / "recoded" / "held.mp4").exists()
    assert not list((tmp_path / "recoded").glob("*_recode*"))


def test_single_mode_with_suffix(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.set_engine("cpu")
    ctl.set_repair("interpolate")
    ctl.set_use_suffix(True)
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    ctl.start(wait=True)
    assert (tmp_path / "recoded" / "held_recode-inter.mp4").exists()
    assert not (tmp_path / "recoded" / "held.mp4").exists()


def test_several_modes_without_suffix_go_to_one_folder_each(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.set_engine("cpu")
    ctl.set_repair_modes(["off", "interpolate", "cut_keep", "cut_stretch"])
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    wait_analysed(ctl)
    assert ctl.start(wait=True)
    out = tmp_path / "recoded"
    for folder in ("cadence", "inter", "keep", "speed"):
        assert (out / folder / "held.mp4").exists(), folder
    item = ctl.state()["items"][0]
    assert [r["variant"] for r in item["results"]] == ["off", "interpolate", "cut_keep", "cut_stretch"]
    assert all(r["status"] in ("done", "copied") for r in item["results"])   # a clean 24p clip is copied
    frames = {f: probe_file(FFMPEG, out / f / "held.mp4").frames for f in ("cadence", "inter", "keep", "speed")}
    assert frames["cadence"] == N and frames["inter"] == N
    assert frames["speed"] == N - len(HELD) and frames["keep"] > N - len(HELD)
    assert item["pct"] == 100.0 and item["status"] == "ready"


def test_several_modes_with_suffix_share_one_folder(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.set_engine("cpu")
    ctl.set_repair_modes(["interpolate", "cut_stretch"])
    ctl.set_use_suffix(True)
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    ctl.start(wait=True)
    names = sorted(p.name for p in (tmp_path / "recoded").glob("*.mp4"))
    assert names == ["held_recode-inter.mp4", "held_recode-speed.mp4"]


def test_variant_names_follow_the_folder_structure(tmp_path):
    job = core.Job(tmp_path / "a" / "b" / "c.mov", tmp_path / "recoded" / "a" / "b" / "c.mov",
                   tmp_path / "recoded", "a/b/c.mov")
    v = core.variant_job(job, "cut_keep", use_suffix=True, multiple=True)
    assert v.dst == tmp_path / "recoded" / "a" / "b" / "c_recode-keep.mov" and v.variant == "cut_keep"
    v = core.variant_job(job, "cut_keep", use_suffix=False, multiple=True)
    assert v.dst == tmp_path / "recoded" / "keep" / "a" / "b" / "c.mov"
    v = core.variant_job(job, "off", use_suffix=False, multiple=False)
    assert v.dst == tmp_path / "recoded" / "a" / "b" / "c.mov"


def test_rerun_with_other_modes_keeps_earlier_results_on_disk(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.set_engine("cpu")
    ctl.set_use_suffix(True)
    ctl.set_repair("interpolate")
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    ctl.start(wait=True)
    ctl.set_repair("cut_stretch")                          # same clip, nothing re-imported
    ctl.start(wait=True)
    names = sorted(p.name for p in (tmp_path / "recoded").glob("*.mp4"))
    assert names == ["held_recode-inter.mp4", "held_recode-speed.mp4"]
    assert ctl.state()["items"][0]["results"][0]["variant"] == "cut_stretch"


def test_log_names_the_variant(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.set_engine("cpu")
    ctl.set_repair_modes(["off", "interpolate"])
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    ctl.start(wait=True)
    log = (tmp_path / "recoded" / core.LOG_NAME).read_text()
    assert "interpolate" in log and "[off]" in log


def test_repair_options_follow_the_modes(tmp_path):
    ctl, _ = make(tmp_path)
    ctl.set_sensitivity("loose")
    ctl.set_engine("gpu")
    assert ctl._repair_options("off") is None
    o = ctl._repair_options("interpolate")
    assert (o.mode, o.sensitivity, o.engine) == ("interpolate", "loose", "gpu")
    o = ctl._repair_options("cut_keep")
    assert (o.mode, o.audio) == ("cut", "keep")
    o = ctl._repair_options("cut_stretch")
    assert (o.mode, o.audio) == ("cut", "stretch")


def test_settings_are_locked_while_running(tmp_path, clips):
    ctl, _ = make(tmp_path)
    ctl.add_paths(copy_in(tmp_path, clips, "ntsc24"))
    wait_probed(ctl)
    ctl.running = True
    ctl.toggle_repair("interpolate")
    ctl.set_use_suffix(True)
    ctl.set_mode(30)
    assert ctl.state()["repair"]["modes"] == ["off"] and not ctl.state()["repair"]["suffix"]
    assert ctl.state()["mode"] == "auto"
    ctl.running = False


# ------------------------------------------------------- manual edits (2.3)
def _analysed(tmp_path, held_clip):
    ctl, _ = make(tmp_path)
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_probed(ctl)
    wait_analysed(ctl)
    return ctl, ctl.state()["items"][0]["id"]


def test_manual_edits_survive_a_new_analysis_pass(tmp_path, held_clip):
    ctl, iid = _analysed(tmp_path, held_clip)
    tl = ctl.set_flags(iid, [5, 11])
    assert [r[0] for r in tl["runs"]] == [5, 11] and tl["manual"] and tl["v"] == ctl.state()["items"][0]["tl_v"]
    ctl.set_auto_analyse(False)
    ctl.set_auto_analyse(True)                             # 2.2.x put the detection back here
    time.sleep(0.5)
    ctl.analyse_now()
    time.sleep(0.5)
    ctl.set_sensitivity("loose")
    item = ctl.state()["items"][0]
    assert item["dups"] == 2 and item["manual"] and ctl._flags[iid]["flagged"] == [5, 11]


def test_reset_flags_goes_back_to_the_detection(tmp_path, held_clip):
    ctl, iid = _analysed(tmp_path, held_clip)
    ctl.set_flags(iid, [3])
    tl = ctl.reset_flags(iid)
    assert [r[0] for r in tl["runs"]] == HELD and not tl["manual"]
    assert not ctl.state()["items"][0]["manual"]


def test_set_flags_validates_its_input(tmp_path, held_clip):
    ctl, iid = _analysed(tmp_path, held_clip)
    assert ctl.set_flags(iid, ["x"]) is None and ctl.set_flags(iid, [None]) is None
    tl = ctl.set_flags(iid, [0, 5, 5, N, 10 ** 9, -1])     # frame 0 has no predecessor
    assert [r[0] for r in tl["runs"]] == [5]
    assert ctl.set_flags(999, [5]) is None
    from framesnap.app import Api

    assert Api.set_flags(type("A", (), {"_c": ctl})(), iid, "5,6") is None


def test_manual_edits_are_used_by_the_run(tmp_path, held_clip):
    ctl, iid = _analysed(tmp_path, held_clip)
    ctl.set_engine("cpu")
    ctl.set_repair("cut_stretch")
    ctl.set_flags(iid, HELD[:3])
    assert ctl.start(wait=True)
    assert probe_file(FFMPEG, tmp_path / "recoded" / "held.mp4").frames == N - 3


def test_changing_the_rate_drops_manual_edits(tmp_path, held_clip):
    ctl, iid = _analysed(tmp_path, held_clip)
    ctl.set_flags(iid, [5])
    ctl.set_mode(30)
    assert not ctl.state()["items"][0]["manual"] and iid not in ctl._flags


def test_api_has_no_unsafe_frame_helpers():
    from framesnap.app import Api

    assert not hasattr(Api, "add_frame") and not hasattr(Api, "remove_frame")
    assert callable(Api.reset_flags) and callable(Api.open_url)


# ------------------------------------------------------- stopping (2.3)
def test_removing_a_clip_stops_its_analysis(tmp_path, held_clip, monkeypatch):
    started, stopped = [], []

    def slow_diffs(ffmpeg, src, vf, w, h, cancel=None, on_progress=None):
        started.append(src)
        end = time.time() + 20
        while time.time() < end:
            if cancel is not None and cancel.is_set():
                stopped.append(src)
                raise core.Cancelled()
            time.sleep(0.02)
        raise AssertionError("the analysis was never stopped")

    monkeypatch.setattr(dedupe, "frame_diffs", slow_diffs)
    ctl, _ = make(tmp_path)
    ctl.add_paths([held_in(tmp_path, held_clip)])
    wait_for(lambda: started)
    ctl.remove(ctl.state()["items"][0]["id"])
    wait_for(lambda: stopped, 5)


def test_adding_then_clearing_at_once_leaves_a_clean_state(tmp_path, clips):
    ctl, _ = make(tmp_path)
    ctl.add_paths(copy_in(tmp_path, clips, "ntsc24", "ntsc30", "fps25"))
    ctl.clear()
    time.sleep(1.5)
    assert ctl.state()["items"] == [] and ctl._probes == {} and ctl._analysis == {}


def test_shutdown_stops_the_encoder_and_leaves_no_partial_file(tmp_path):
    clip = tmp_path / "big.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=30000/1001", "-t", "6", "-c:v", "libx264",
                    "-preset", "ultrafast", str(clip)], check=True)
    ctl = Controller(lambda s: None, check_gpu=False,
                     recoder_factory=lambda **k: Recoder(FFMPEG, **k), preset="veryslow", crf=12)
    ctl.add_paths([str(clip)])
    wait_probed(ctl)
    assert ctl.start()
    wait_for(lambda: core.live_processes(), 20)            # the encoder is running
    t0 = time.time()
    ctl.shutdown()
    assert time.time() - t0 < 10
    assert core.live_processes() == [] and not ctl._worker.is_alive()
    assert not list(tmp_path.rglob("*.part.*")) and not (tmp_path / "recoded" / "big.mp4").exists()
    assert ctl.add_paths([str(clip)]) == 0 and not ctl.start()
