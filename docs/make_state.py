"""Build real application states (and timelines) for the README screenshots.

Usage:
    python docs/make_state.py OUT_DIR "clip A.mp4=shot_01.mp4" "clip B.mp4=shot_02.mp4" ...

Every "path=display name" pair is analysed for real by the Controller; the display name only
replaces the file name in the output, so that private project names never end up in a
public repository. Two states are written:

    OUT_DIR/state_ready.json     clips analysed, nothing encoded yet
    OUT_DIR/state_done.json      the same after a real run (two outputs per clip)
    OUT_DIR/timelines.json       the timeline data of every clip
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framesnap.app import Controller  # noqa: E402
from framesnap.core import Recoder, find_ffmpeg  # noqa: E402


def wait(cond, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.1)
    raise SystemExit("timeout")


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    pairs = [a.split("=", 1) for a in sys.argv[2:]]
    work = Path(tempfile.mkdtemp(prefix="framesnap-shots-"))
    for src, display in pairs:
        shutil.copy(src, work / display)
    ffmpeg = find_ffmpeg()
    ctl = Controller(lambda s: None, check_gpu=True,
                     recoder_factory=lambda **k: Recoder(ffmpeg, **k), preset="ultrafast", crf=22)
    ctl.set_engine("auto")
    ctl.add_paths([str(work)])
    wait(lambda: ctl.state()["items"] and all(i["status"] != "probing" for i in ctl.state()["items"]))
    wait(lambda: all(i["analysis"] in ("done", "skip", "error") for i in ctl.state()["items"]))
    wait(lambda: ctl.state()["repair"]["gpu"].get("checked"))
    ctl.set_repair_modes(["interpolate", "cut_stretch"])
    ctl.set_use_suffix(True)
    ready = ctl.state()
    json.dump(ready, open(out / "state_ready.json", "w"), indent=1)
    tls = {str(i["id"]): ctl.get_timeline(i["id"]) for i in ready["items"]}
    json.dump(tls, open(out / "timelines.json", "w"))
    ctl.start(wait=True)
    done = ctl.state()
    json.dump(done, open(out / "state_done.json", "w"), indent=1)
    for i in done["items"]:
        print(i["name"], "fps=%.3f" % i["fps"], "dups=", i["dups"], i["runs"],
              [(r["variant"], r["status"], r["backend"]) for r in i["results"]])
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
