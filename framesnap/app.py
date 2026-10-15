"""FrameSnap desktop app: a small pywebview window driven by a Controller.

The Controller holds all the state and logic and knows nothing about the GUI,
so it is fully testable. The Api class is the thin object exposed to the page.

Threads: probing, analysis and encoding run in daemon threads. ``Controller.shutdown``
(called when the window closes) cancels them, kills their ffmpeg processes and waits
for them, so that nothing keeps running and no ``.part`` file is left behind.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

from . import __version__
from . import preview
from .core import (
    STANDARD_RATES, VARIANT_ORDER, VARIANTS, VIDEO_EXTS, Cancelled, Job, JobResult, Plan, Probe,
    Recoder, bundle_roots, collect_jobs, expected_frames, find_ffmpeg, kill_all, make_plan,
    probe_file, run_capture,
)

Mode = Union[str, int]
PUSH_INTERVAL = 0.2

GITHUB_REPO = "le-wib/framesnap"
RELEASES_URL = "https://github.com/%s/releases" % GITHUB_REPO
NO_UPDATE_CHECK_ENV = "FRAMESNAP_NO_UPDATE_CHECK"


# --------------------------------------------------------------------------
# update check
# --------------------------------------------------------------------------

def parse_version(text: Optional[str]) -> Optional[Tuple[int, ...]]:
    """"v2.3.0" -> (2, 3, 0). None when the text is not a version."""
    if not text:
        return None
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", str(text))
    if not m:
        return None
    parts = tuple(int(x) for x in m.group(1).split("."))
    return parts + (0,) * (3 - len(parts)) if len(parts) < 3 else parts


def is_newer(latest: Optional[str], current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def _fetch_latest_release(timeout: float = 5.0) -> Optional[dict]:
    """{"tag", "url"} of the latest GitHub release, or None (offline, error, disabled).

    This is the only request FrameSnap sends over the internet. Set the environment
    variable FRAMESNAP_NO_UPDATE_CHECK=1 to switch it off.
    """
    if os.environ.get(NO_UPDATE_CHECK_ENV):
        return None
    try:
        url = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
        req = urllib.request.Request(url, headers={
            "User-Agent": "FrameSnap/%s" % __version__, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not parse_version(tag):
        return None
    page = data.get("html_url") or ""
    if not (isinstance(page, str) and page.startswith(RELEASES_URL + "/")):
        page = "%s/tag/%s" % (RELEASES_URL, tag)
    return {"tag": tag, "url": page}


def check_for_update(current: str = __version__) -> dict:
    """What the page needs to show the update banner."""
    rel = _fetch_latest_release()
    if rel is None:
        return {"current": current, "latest": None, "update_available": False,
                "url": RELEASES_URL, "error": True}
    latest = ".".join(str(x) for x in parse_version(rel["tag"]) or ())
    return {"current": current, "latest": latest, "update_available": is_newer(rel["tag"], current),
            "url": rel["url"], "error": False}


def safe_release_url(url: str) -> bool:
    """Only the project's release pages may be opened from the page."""
    return isinstance(url, str) and (url == RELEASES_URL or url.startswith(RELEASES_URL + "/"))


# --------------------------------------------------------------------------
# controller
# --------------------------------------------------------------------------

class Controller:
    """All the app state. ``push(state_dict)`` is called after every change."""

    REPAIR_MODES = VARIANT_ORDER            # off (rate only), interpolate, cut_keep, cut_stretch
    SENSITIVITIES = ("strict", "normal", "loose")
    ENGINES = ("auto", "gpu", "cpu")

    def __init__(self, push: Callable[[dict], None],
                 recoder_factory: Callable[..., Recoder] = Recoder,
                 preset: str = "slow", crf: int = 12, check_gpu: bool = True):
        self._push_cb = push
        self._factory = recoder_factory
        self._preset = preset
        self._crf = crf
        self._lock = threading.RLock()
        self._an_lock = threading.Lock()          # one analysis at a time
        self._an_cancel = threading.Event()       # stops the analysis in progress
        self._an_current: Optional[int] = None
        self._items: List[dict] = []
        self._jobs: Dict[int, Job] = {}
        self._probes: Dict[int, Probe] = {}
        self._analysis: Dict[int, dict] = {}      # iid -> {"key", "diffs", "fps"}
        self._flags: Dict[int, dict] = {}         # iid -> {"sens", "key", "flagged", "manual"}
        self._processed: set = set()              # "<iid>|<variant>" written in this session
        self._next_id = 1
        self.mode: Mode = "auto"
        self.skip_existing = True
        self.running = False
        self.closing = False
        self._recoder: Optional[Recoder] = None
        self._worker: Optional[threading.Thread] = None
        self._last_push = 0.0
        self._error: str = ""
        self.repair_modes: List[str] = ["off"]
        self.use_suffix = False
        self.auto_analyse = True
        self.sensitivity = "normal"
        self.engine = "auto"
        self._gpu: dict = {"checked": False}
        if check_gpu:
            threading.Thread(target=self._check_gpu, daemon=True).start()
        else:
            self._gpu = {"checked": True, "available": False, "ok": False, "device": "",
                         "software": False, "error": "not checked"}

    # ------------------------------------------------------------ state
    def state(self) -> dict:
        with self._lock:
            return {
                "version": __version__,
                "mode": self.mode,
                "skip_existing": self.skip_existing,
                "running": self.running,
                "error": self._error,
                "repair": {
                    "modes": list(self.repair_modes), "sensitivity": self.sensitivity,
                    "engine": self.engine, "gpu": dict(self._gpu),
                    "suffix": self.use_suffix, "auto_analyse": self.auto_analyse,
                    "suffixes": {k: v[0] for k, v in VARIANTS.items()},
                    "folders": {k: v[1] for k, v in VARIANTS.items()},
                },
                "items": [dict(i) for i in self._items],
            }

    def _push(self, force: bool = True) -> None:
        """Send the state to the GUI. Never call it while holding ``self._lock``."""
        now = time.time()
        if not force and now - self._last_push < PUSH_INTERVAL:
            return
        self._last_push = now
        try:
            self._push_cb(self.state())
        except Exception:  # noqa: BLE001  (a broken GUI must never kill a job)
            pass

    def _rec(self) -> Recoder:
        if self._recoder is None:
            self._recoder = self._factory(preset=self._preset, crf=self._crf)
        return self._recoder

    # ------------------------------------------------------------ queue
    def add_paths(self, paths: List[str]) -> int:
        """Add files and folders. Probing and analysis happen in the background."""
        if self.running or self.closing:
            return 0
        try:
            self._rec()
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            self._push()
            return 0
        self._error = ""
        jobs = collect_jobs(paths)
        with self._lock:
            known = {str(j.src) for j in self._jobs.values()}
            new: List[Tuple[int, Job]] = []
            for job in jobs:
                if str(job.src) in known:
                    continue
                iid = self._next_id
                self._next_id += 1
                self._jobs[iid] = job
                new.append((iid, job))
                self._items.append({
                    "id": iid, "name": job.rel, "status": "probing",
                    "fps": None, "target": None, "action": "", "pct": 0.0,
                    "message": "", "w": 0, "h": 0,
                    "analysis": "pending" if self.auto_analyse else "off",
                    "analysis_pct": 0.0, "dups": None, "dup_pct": 0.0,
                    "runs": {"double": 0, "triple": 0, "more": 0}, "tl_v": 0,
                    "manual": False, "results": [],
                })
        self._push()
        if new:
            threading.Thread(target=self._probe_many, args=(new,), daemon=True).start()
        return len(new)

    def _item(self, iid: int) -> Optional[dict]:
        for it in self._items:
            if it["id"] == iid:
                return it
        return None

    def _probe_many(self, new: List[Tuple[int, Job]]) -> None:
        rec = self._rec()
        for iid, job in new:
            if self.closing:
                return
            with self._lock:
                if iid not in self._jobs:          # removed while waiting
                    continue
            try:
                probe = rec.probe(job.src)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    it = self._item(iid)
                    if it:
                        it.update(status="error", message=str(exc), analysis="off")
                self._push(force=False)
                continue
            with self._lock:
                it = self._item(iid)
                if it is None or iid not in self._jobs:
                    continue
                self._probes[iid] = probe
                it.update(fps=probe.fps, w=probe.width, h=probe.height)
                self._apply_plan(it, probe)
            self._push(force=False)
        # analysis starts once every clip of the batch is probed, so the list fills quickly
        if self.auto_analyse:
            for iid, _ in new:
                if self.closing:
                    return
                if iid in self._probes:
                    self._analyse_item(iid)
        self._push()

    def _check_gpu(self) -> None:
        try:
            from . import rife

            r = rife.gpu_check()
        except Exception as exc:  # noqa: BLE001
            r = {"available": False, "ok": False, "device": "", "software": False,
                 "error": str(exc)}
        r = dict(r)
        r["checked"] = True
        self._gpu = r
        self._push()

    def _apply_plan(self, it: dict, probe: Probe) -> None:
        plan = make_plan(probe, self.mode, self._rec().tolerance)
        it.update(status="ready", action=plan.action, target=plan.target,
                  pct=0.0, message="")

    # ------------------------------------------------------------ analysis
    @staticmethod
    def _plan_key(plan: Plan) -> str:
        return "%s:%s" % (plan.action, plan.target)

    def _analyse_item(self, iid: int) -> None:
        """Find the held frames of one clip. Decodes at low resolution, once per rate plan."""
        from . import dedupe

        with self._an_lock:
            if self.closing:
                return
            with self._lock:
                it, probe, job = self._item(iid), self._probes.get(iid), self._jobs.get(iid)
                if not (it and probe and job):
                    return
                plan = make_plan(probe, self.mode, self._rec().tolerance)
                key = self._plan_key(plan)
                cached = self._analysis.get(iid)
                if plan.action == "skip":
                    it.update(analysis="skip", dups=None, dup_pct=0.0)
                    todo = False
                elif cached and cached["key"] == key:
                    self._apply_flags(iid)             # keeps manual edits made for this plan
                    todo = False
                else:
                    it.update(analysis="running", analysis_pct=0.0)
                    todo = True
                self._an_cancel.clear()
                self._an_current = iid if todo else None
            self._push()
            if not todo:
                return
            expected = expected_frames(probe, plan)

            def progress(done: int) -> None:
                with self._lock:
                    cur = self._item(iid)
                    if cur:
                        cur["analysis_pct"] = round(min(99.0, 100.0 * done / expected), 1)
                self._push(force=False)

            try:
                diffs = dedupe.frame_diffs(self._rec().ffmpeg, job.src, dedupe.decode_filter(plan),
                                           probe.width, probe.height, self._an_cancel, progress)
            except Cancelled:
                with self._lock:
                    self._an_current = None
                    cur = self._item(iid)
                    if cur:
                        cur.update(analysis="pending" if self.auto_analyse else "off",
                                   analysis_pct=0.0)
                if not self.closing:
                    self._push()
                return
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._an_current = None
                    cur = self._item(iid)
                    if cur:
                        cur.update(analysis="error", message="analysis failed: %s" % exc)
                self._push()
                return
            with self._lock:
                self._an_current = None
                if self._item(iid) is None:
                    return
                self._analysis[iid] = {"key": key, "diffs": diffs,
                                       "fps": float(plan.target or probe.fps)}
                self._apply_flags(iid)
            self._push()

    def _apply_flags(self, iid: int) -> None:
        """Detection from the stored differences: instant, no decoding. Lock must be held.

        Manual edits made for the same rate plan are kept; ``reset_flags`` drops them.
        """
        from . import dedupe

        it, a = self._item(iid), self._analysis.get(iid)
        if not it or not a:
            return
        cur = self._flags.get(iid)
        if cur and cur.get("manual") and cur.get("key") == a["key"]:
            flagged = list(cur["flagged"])
            manual = True
        else:
            flagged = dedupe.detect_duplicates(a["diffs"], self.sensitivity)
            self._flags[iid] = {"sens": self.sensitivity, "key": a["key"], "flagged": flagged}
            manual = False
        n = len(a["diffs"]) + 1
        it.update(analysis="done", analysis_pct=100.0, dups=len(flagged),
                  dup_pct=round(100.0 * len(flagged) / max(1, n), 1),
                  runs=dedupe.run_counts(flagged), manual=manual)
        it["tl_v"] = it.get("tl_v", 0) + 1

    def _analyse_all(self) -> None:
        for iid in [i["id"] for i in list(self._items)]:
            if self.closing:
                return
            if iid in self._probes:
                self._analyse_item(iid)
        self._push()

    def analyse_now(self) -> None:
        """Analysis on demand (when automatic analysis is switched off)."""
        if self.closing:
            return
        threading.Thread(target=self._analyse_all, daemon=True).start()

    def _stop_analysis(self, iid: Optional[int] = None) -> None:
        """Stop the analysis in progress (of ``iid`` only, when given)."""
        with self._lock:
            if self._an_current is not None and (iid is None or self._an_current == iid):
                self._an_cancel.set()

    def get_timeline(self, iid: int) -> Optional[dict]:
        """Motion curve and held frame runs of one analysed clip, for the timeline."""
        from . import dedupe

        with self._lock:
            it = self._item(int(iid))
            a, f = self._analysis.get(int(iid)), self._flags.get(int(iid))
            if not a or not f or not it:
                return None
            tl = dedupe.timeline_data(a["diffs"], f["flagged"], a["fps"])
            tl.update(v=it["tl_v"], manual=bool(f.get("manual")))
            return tl

    # ------------------------------------------------------------ manual flag editing
    def set_flags(self, iid: int, flagged: List[int]) -> Optional[dict]:
        """Replace the held frame list of one clip (manual edits from the page).

        Frame 0 cannot be held (it has no predecessor). Returns the new timeline, or None
        when the clip is not analysed or the list is not a list of frame numbers.
        """
        try:
            iid = int(iid)
            wanted = sorted({int(f) for f in flagged})
        except (TypeError, ValueError):
            return None
        with self._lock:
            it = self._item(iid)
            a = self._analysis.get(iid)
            if not it or not a:
                return None
            n = len(a["diffs"]) + 1
            clean = [f for f in wanted if 1 <= f < n]
            existing = self._flags.get(iid, {})
            self._flags[iid] = {"sens": existing.get("sens", self.sensitivity),
                                "key": a["key"], "flagged": clean, "manual": True}
            self._apply_flags(iid)
        self._push()
        return self.get_timeline(iid)

    def reset_flags(self, iid: int) -> Optional[dict]:
        """Drop the manual edits of one clip: back to the automatic detection."""
        iid = int(iid)
        with self._lock:
            if iid not in self._analysis:
                return None
            self._flags.pop(iid, None)
            self._apply_flags(iid)
        self._push()
        return self.get_timeline(iid)

    def get_video_path(self, iid: int) -> Optional[str]:
        """Absolute path of the source clip (served to the preview)."""
        with self._lock:
            job = self._jobs.get(int(iid))
            return str(job.src) if job else None

    # ------------------------------------------------------------ settings
    def set_repair_modes(self, modes: List[str]) -> None:
        """Which outputs to write for every clip (at least one)."""
        if self.running:
            return
        wanted = [m for m in self.REPAIR_MODES if m in set(modes)]
        if not wanted or wanted == self.repair_modes:
            return
        self.repair_modes = wanted
        self._push()

    def toggle_repair(self, mode: str) -> None:
        if mode not in self.REPAIR_MODES:
            return
        cur = list(self.repair_modes)
        if mode in cur:
            if len(cur) == 1:
                return                              # keep at least one output
            cur.remove(mode)
        else:
            cur.append(mode)
        self.set_repair_modes(cur)

    def set_repair(self, mode: str) -> None:
        """Convenience: exactly one output."""
        if mode in self.REPAIR_MODES:
            self.set_repair_modes([mode])

    def set_use_suffix(self, value: bool) -> None:
        if self.running:
            return
        self.use_suffix = bool(value)
        self._push()

    def set_auto_analyse(self, value: bool) -> None:
        self.auto_analyse = bool(value)
        with self._lock:
            for it in self._items:
                if it["analysis"] in ("pending", "off") and it["id"] in self._probes:
                    it["analysis"] = "pending" if self.auto_analyse else "off"
        self._push()
        if self.auto_analyse:
            self.analyse_now()

    def set_sensitivity(self, value: str) -> None:
        if self.running or value not in self.SENSITIVITIES or value == self.sensitivity:
            return
        self.sensitivity = value
        with self._lock:
            for iid in list(self._analysis):
                self._apply_flags(iid)             # instant; manual edits are kept
        self._push()

    def set_engine(self, value: str) -> None:
        if self.running or value not in self.ENGINES:
            return
        self.engine = value
        self._push()

    def set_mode(self, mode: Mode) -> None:
        if self.running:
            return
        if mode != "auto":
            try:
                mode = int(mode)
            except (TypeError, ValueError):
                return
            if mode not in STANDARD_RATES:
                return
        if mode == self.mode:
            return
        self._stop_analysis()                       # its frames belong to the old rate
        with self._lock:
            self.mode = mode
            self._flags.clear()                    # frame numbers change with the rate
            for it in self._items:
                probe = self._probes.get(it["id"])
                if probe is not None and it["status"] != "probing":
                    self._apply_plan(it, probe)
                    it.update(dups=None, dup_pct=0.0, manual=False,
                              analysis="pending" if self.auto_analyse else "off")
        self._push()
        if self.auto_analyse:
            self.analyse_now()

    def set_skip_existing(self, value: bool) -> None:
        self.skip_existing = bool(value)
        self._push()

    def remove(self, iid: int) -> None:
        if self.running:
            return
        self._stop_analysis(iid)
        with self._lock:
            self._items = [i for i in self._items if i["id"] != iid]
            for table in (self._jobs, self._probes, self._flags, self._analysis):
                table.pop(iid, None)
        self._push()

    def clear(self) -> None:
        if self.running:
            return
        self._stop_analysis()
        with self._lock:
            self._items.clear()
            for table in (self._jobs, self._probes, self._flags, self._analysis):
                table.clear()
            self._processed.clear()
        self._push()

    # ------------------------------------------------------------ run
    def start(self, wait: bool = False) -> bool:
        """Write every selected output for every clip. Can be called again and again:
        nothing has to be imported twice."""
        if self.closing:
            return False
        rec = self._rec()
        with self._lock:
            if self.running:
                return False
            todo = [i for i in self._items if i["status"] == "ready" and i["id"] in self._probes]
            if not todo:
                return False
            self.running = True
            rec.reset()                            # before the thread: a Cancel from now on is kept
            for it in todo:
                it.update(status="queued", pct=0.0, message="", results=[])
        self._push()
        t = threading.Thread(target=self._run, args=([i["id"] for i in todo],), daemon=True)
        self._worker = t
        t.start()
        if wait:
            t.join()
        return True

    def _run(self, ids: List[int]) -> None:
        rec = self._rec()
        rec.skip_existing = self.skip_existing
        modes = list(self.repair_modes)
        variants = [(m, self._repair_options(m)) for m in modes]
        flags = {}
        force = set()
        with self._lock:
            for i in ids:
                src = str(self._jobs[i].src)
                c = self._flags.get(i)
                plan = make_plan(self._probes[i], self.mode, rec.tolerance)
                if c and c["key"] == self._plan_key(plan) and (
                        c.get("manual") or c["sens"] == self.sensitivity):
                    flags[src] = c["flagged"]
                for m in modes:           # redone on purpose: the user pressed Recode again
                    if "%d|%s" % (i, m) in self._processed:
                        force.add("%s|%s" % (src, m))
            jobs = [self._jobs[i] for i in ids]
            by_src = {str(self._jobs[i].src): i for i in ids}

        def on_event(job: Job, kind: str, info: dict) -> None:
            iid = by_src[str(job.src)]
            idx, total = info.get("index", 0), max(1, info.get("total", 1))
            with self._lock:
                it = self._item(iid)
                if not it:
                    return
                if kind == "start":
                    it.update(status="running", pct=round(100.0 * idx / total, 1))
                elif kind == "progress":
                    it["pct"] = round(100.0 * (idx + info["pct"] / 100.0) / total, 1)
                elif kind == "result":
                    res: JobResult = info["result"]
                    entry = {"variant": res.variant, "status": res.status, "message": res.message,
                             "backend": res.backend, "dups": res.dups, "file": job.dst.name}
                    it["results"] = [r for r in it["results"] if r["variant"] != res.variant] + [entry]
                    if res.status in ("done", "copied"):
                        self._processed.add("%d|%s" % (iid, res.variant))
                    it["pct"] = round(100.0 * (idx + 1) / total, 1)
                    if idx == total - 1:
                        it["status"] = "ready"       # ready to be run again
            self._push(force=(kind != "progress"))

        try:
            rec.run(jobs, self.mode, on_event, overwrite=force, flags=flags,
                    variants=variants, use_suffix=self.use_suffix, reset=False)
        finally:
            with self._lock:
                self.running = False
                for it in self._items:
                    if it["status"] in ("queued", "running"):
                        it["status"] = "ready"      # cancelled: can be started again
            if not self.closing:
                self._push()

    def _repair_options(self, mode: str):
        if mode == "off":
            return None
        from .dedupe import RepairOptions

        if mode == "interpolate":
            return RepairOptions("interpolate", sensitivity=self.sensitivity, engine=self.engine)
        return RepairOptions("cut", "keep" if mode == "cut_keep" else "stretch",
                             sensitivity=self.sensitivity, engine=self.engine)

    def cancel(self) -> None:
        if self.running:
            self._rec().cancel()

    def stop_now(self) -> None:
        """Window closing: stop everything at once, without waiting (safe from the GUI thread)."""
        self.closing = True
        self._an_cancel.set()
        if self._recoder is not None:
            self._recoder.cancel()
        kill_all(timeout=0.5)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Stop the analysis and the encoding, kill their processes, wait for the threads."""
        self.stop_now()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout)
        kill_all()

    def output_dirs(self) -> List[str]:
        with self._lock:
            roots = []
            for it in self._items:
                job = self._jobs.get(it["id"])
                if job and job.out_root.exists() and str(job.out_root) not in roots:
                    roots.append(str(job.out_root))
            return roots


# --------------------------------------------------------------------------
# the object exposed to the page
# --------------------------------------------------------------------------

class Api:
    """Object exposed to the page as window.pywebview.api."""

    def __init__(self) -> None:
        self._window = None
        self._closed = False
        self._c = Controller(self._push)

    def _attach(self, window) -> None:
        self._window = window

    def _push(self, state: dict) -> None:
        if self._window is not None and not self._closed:
            self._window.evaluate_js("window.render(%s)" % json.dumps(state))

    # --- called by the page
    def get_state(self) -> dict:
        return self._c.state()

    def check_update(self) -> dict:
        """Update check, asked by the page once at start and by the refresh button."""
        return check_for_update()

    def open_url(self, url) -> bool:
        """Open a release page of FrameSnap in the browser (any other address is refused)."""
        if not safe_release_url(url):
            return False
        import webbrowser

        return bool(webbrowser.open(url))

    def add_paths(self, paths: List[str]) -> int:
        return self._c.add_paths([p for p in paths if p])

    def pick_files(self) -> int:
        import webview

        kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None)
        if kind is None:
            kind = webview.OPEN_DIALOG
        types = ("Video (%s)" % ";".join("*" + e for e in sorted(VIDEO_EXTS)),)
        res = self._window.create_file_dialog(kind, allow_multiple=True, file_types=types)
        return self._c.add_paths(list(res)) if res else 0

    def pick_folder(self) -> int:
        import webview

        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if kind is None:
            kind = webview.FOLDER_DIALOG
        res = self._window.create_file_dialog(kind)
        return self._c.add_paths(list(res)) if res else 0

    def set_mode(self, mode) -> None:
        self._c.set_mode(mode)

    def set_skip_existing(self, value) -> None:
        self._c.set_skip_existing(value)

    def toggle_repair(self, mode) -> None:
        self._c.toggle_repair(str(mode))

    def set_repair(self, mode) -> None:
        self._c.set_repair(str(mode))

    def set_use_suffix(self, value) -> None:
        self._c.set_use_suffix(bool(value))

    def set_auto_analyse(self, value) -> None:
        self._c.set_auto_analyse(bool(value))

    def analyse_now(self) -> None:
        self._c.analyse_now()

    def get_timeline(self, iid):
        return self._c.get_timeline(int(iid))

    def set_flags(self, iid, flagged) -> Optional[dict]:
        """Replace the held frame list of clip ``iid`` (manual add or remove in the page)."""
        if not isinstance(flagged, (list, tuple)):
            return None
        return self._c.set_flags(int(iid), list(flagged))

    def reset_flags(self, iid) -> Optional[dict]:
        return self._c.reset_flags(int(iid))

    def get_video_path(self, iid) -> Optional[str]:
        """A secret http://127.0.0.1 URL the <video> element can stream (see preview.py)."""
        path = self._c.get_video_path(int(iid))
        if path is None:
            return None
        return preview.get_server().url_for(path)

    def set_sensitivity(self, value) -> None:
        self._c.set_sensitivity(str(value))

    def set_engine(self, value) -> None:
        self._c.set_engine(str(value))

    def start(self) -> bool:
        return self._c.start()

    def cancel(self) -> None:
        self._c.cancel()

    def clear(self) -> None:
        self._c.clear()
        if not self._c.running:
            preview.revoke()

    def remove(self, iid) -> None:
        path = self._c.get_video_path(int(iid))
        self._c.remove(int(iid))
        if path and self._c.get_video_path(int(iid)) is None:
            preview.revoke(path)

    def open_output(self) -> None:
        for d in self._c.output_dirs()[:5]:
            _open_path(d)

    # --- lifecycle (not called by the page)
    def _stop_now(self, *_) -> None:
        self._closed = True
        self._c.stop_now()

    def _shutdown(self) -> None:
        self._closed = True
        self._c.shutdown()
        preview.shutdown()


def _open_path(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])


# --------------------------------------------------------------------------
# window
# --------------------------------------------------------------------------

def resource_dir() -> Path:
    for root in bundle_roots():
        p = Path(root) / "framesnap" / "ui"
        if (p / "index.html").exists():
            return p
    return Path(__file__).resolve().parent / "ui"


def selftest() -> int:
    """Encode a synthetic NTSC clip with the bundled ffmpeg and check the result."""
    work = Path(tempfile.mkdtemp(prefix="framesnap-selftest-"))
    try:
        ffmpeg = find_ffmpeg()
        src = work / "selftest.mp4"
        r = run_capture([
            ffmpeg, "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30000/1001",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(src),
        ])
        if r.returncode != 0:
            return _selftest_report(False, "ffmpeg cannot encode: %s" % (r.stderr or "").strip()[-200:])
        rec = Recoder(ffmpeg, preset="ultrafast")
        res = rec.run(collect_jobs([src]), "auto")[0]
        out = probe_file(ffmpeg, res.job.dst)
        ok = (res.status == "done" and res.target == 30 and out.uniform
              and abs(out.fps - 30) < 1e-6 and out.has_audio)
        msg = "ffmpeg: %s, %s, %s fps" % (ffmpeg, res.status, out.fps)
        if not ok:
            return _selftest_report(False, msg + " " + res.message)
        extra = _selftest_repair(ffmpeg, src, work)
        if extra.startswith("FAILED"):
            return _selftest_report(False, msg + " | " + extra)
        msg += " | " + extra
        served = _selftest_preview(src)
        if served.startswith("FAILED"):
            return _selftest_report(False, msg + " | " + served)
        msg += " | " + served
        if (sys.platform == "darwin" or sys.platform.startswith("win")
                or os.environ.get("FRAMESNAP_SELFTEST_GUI")):
            import webview  # noqa: F401
            from webview.dom import DOMEventHandler  # noqa: F401

            if sys.platform == "darwin":
                import webview.platforms.cocoa  # noqa: F401
            elif sys.platform.startswith("win"):
                import webview.platforms.winforms  # noqa: F401
                import webview.platforms.edgechromium  # noqa: F401
            if not (resource_dir() / "index.html").exists():
                return _selftest_report(False, "ui files missing: %s" % resource_dir())
            msg += ", gui ok, ui=%s" % resource_dir()
        return _selftest_report(True, msg)
    except Exception as exc:  # noqa: BLE001
        return _selftest_report(False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        preview.shutdown()
        kill_all()
        shutil.rmtree(work, ignore_errors=True)


def _selftest_preview(src: Path) -> str:
    """The preview server answers a range request with the right bytes."""
    url = preview.get_server().url_for(str(src))
    req = urllib.request.Request(url, headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read()
        status = resp.status
    with open(src, "rb") as fh:
        want = fh.read(100)
    if status != 206 or data != want:
        return "FAILED preview server: status %s, %d bytes" % (status, len(data))
    return "preview ok"


def _selftest_repair(ffmpeg: str, src: Path, work: Path) -> str:
    """Held frame repair with the CPU engine, and a look at the bundled RIFE (informational)."""
    import numpy as np

    from . import dedupe, rife

    probe = probe_file(ffmpeg, src)
    plan = Recoder(ffmpeg).plan(probe, "auto")
    out = work / "repaired.mp4"
    rep = dedupe.repair(ffmpeg, src, out, probe, plan,
                        dedupe.RepairOptions("interpolate", engine="cpu"),
                        preset="ultrafast", crf=18, flagged=[5, 6])
    after = probe_file(ffmpeg, out)
    if rep.repaired != 2 or after.frames != probe.frames:
        return "FAILED repair: %s frames, %s repaired" % (after.frames, rep.repaired)
    mid = dedupe.Interpolator("cpu").between(np.zeros((64, 64, 3), np.uint8),
                                             np.full((64, 64, 3), 200, np.uint8), [0.5])[0]
    if not 60 < float(mid.mean()) < 140:
        return "FAILED optical flow: mean %.1f" % float(mid.mean())
    exe = rife.find_rife()
    if exe is None:
        if getattr(sys, "frozen", False):
            return "FAILED rife is missing from the packaged app"
        return "repair ok, rife: not installed (CPU engine only)"
    starts = False
    if rife.vulkan_runtime_present():
        try:
            r = run_capture([str(exe)], timeout=60)
            starts = "Usage" in ((r.stdout or "") + (r.stderr or ""))
        except Exception:  # noqa: BLE001
            starts = False
    gpu = rife.gpu_check(force=True)
    return "repair ok, rife %s, gpu: %s%s" % (
        "starts" if starts else "present but cannot start here (CPU engine will be used)",
        gpu["device"] or "none", " (software)" if gpu["software"] else "")


def _selftest_report(ok: bool, message: str) -> int:
    line = "selftest %s: %s" % ("OK" if ok else "FAILED", message)
    log = os.environ.get("FRAMESNAP_SELFTEST_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    try:
        print(line, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
    return 0 if ok else 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    import webview
    from webview.dom import DOMEventHandler

    html = (resource_dir() / "index.html").read_text(encoding="utf-8")
    api = Api()
    window = webview.create_window(
        "FrameSnap", html=html, js_api=api, width=560, height=980,
        min_size=(480, 700), background_color="#0f1013",
    )
    api._attach(window)
    try:
        window.events.closed += api._stop_now       # stops encoders even if start() hangs
    except Exception:  # noqa: BLE001
        pass

    def on_drop(event: dict) -> None:
        files = (event.get("dataTransfer") or {}).get("files", [])
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        if paths:
            api.add_paths(paths)

    def bind() -> None:
        window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)

    try:
        if sys.platform.startswith("win"):
            webview.start(bind, gui="edgechromium")
        else:
            webview.start(bind)
    except Exception as exc:  # noqa: BLE001
        _fatal("FrameSnap could not open its window.\n\n%s\n\n"
               "On Windows this usually means the Microsoft Edge WebView2 Runtime is "
               "missing: https://go.microsoft.com/fwlink/p/?LinkId=2124703" % exc)
        raise
    finally:
        api._shutdown()


def _fatal(message: str) -> None:
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, "FrameSnap", 0x10)  # type: ignore[attr-defined]
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
