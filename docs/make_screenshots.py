#!/usr/bin/env python3
"""Draws the README screenshots (docs/screenshots/*.png) with the real FrameSnap.

    pip install playwright && python -m playwright install chromium
    python docs/make_screenshots.py STILL_IMAGE [--out docs/screenshots]

STILL_IMAGE is one frame of a clip (the 2.3 screenshots use frame 219 of the rush
dialogue-dreamina). Three short demo clips are made from it (slow camera move, a few held
frames added), then the real controller probes, analyses and recodes them (CPU engine) and
the real page is drawn in Chromium, with the real preview server streaming the clip.

For the preview screenshot, the false detections are removed by hand (set_flags), so it
shows the "edited by hand" badge. Run from the repository root. FRAMESNAP_CHROMIUM may name
a Chromium executable; FRAMESNAP_FFMPEG works as in the app.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from framesnap import core, preview  # noqa: E402
from framesnap.app import Controller  # noqa: E402

# name, frames, rate, size, crop box (fractions of the still), zoom, pan (px), held frames
CLIPS = [
    ("dialogue-dreamina.mp4", 240, "24000/1001", (1920, 872), (0, 0, 1, 1), 0.10, (-60, 10),
     {37, 38, 96, 141, 188, 219}),
    ("gardes-kling.mp4", 180, "30000/1001", (1280, 720), (0.39, 0.06, 0.54, 0.25), 0.12, (30, 5),
     {22, 23, 24, 70, 130}),
    ("cantine-seedance.mp4", 150, "25", (1280, 720), (0.16, 0.6, 0.54, 1), 0.08, (-20, -5),
     {44, 101}),
]
SELECTED, FRAME = "dialogue-dreamina.mp4", 219


def make_clip(ffmpeg, img, path, n, rate, size, box, zoom, pan, held, seed):
    """A slow push in with a little hand shake; the held frames repeat the previous one."""
    rng = np.random.default_rng(seed)
    h0, w0 = img.shape[:2]
    w, h = size
    x0, y0, x1, y1 = box[0] * w0, box[1] * h0, box[2] * w0, box[3] * h0
    cmd = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "%dx%d" % (w, h),
           "-r", rate, "-i", "-", "-f", "lavfi", "-i", "sine=frequency=180:sample_rate=48000,volume=0.05",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-c:a", "aac", "-t", "%.6f" % (n / float(Fraction(rate))), "-movflags", "+faststart", str(path)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    last = None
    for i in range(n):
        if i in held and last is not None:
            f = last
        else:
            t, z = i / (n - 1), 1 + zoom * i / (n - 1)
            cx = (x0 + x1) / 2 + pan[0] * t + rng.normal(0, 0.8)
            cy = (y0 + y1) / 2 + pan[1] * t + rng.normal(0, 0.6)
            bw, bh = (x1 - x0) / z, (y1 - y0) / z
            m = cv2.getAffineTransform(
                np.float32([[cx - bw / 2, cy - bh / 2], [cx + bw / 2, cy - bh / 2], [cx - bw / 2, cy + bh / 2]]),
                np.float32([[0, 0], [w, 0], [0, h]]))
            f = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        p.stdin.write(f.tobytes())
        last = f
    p.stdin.close()
    if p.wait() != 0:
        raise SystemExit("ffmpeg failed on " + str(path))


def collect(folder):
    """Runs the real controller and returns the states and timelines to draw."""
    c = Controller(push=lambda s: None, preset="ultrafast", crf=18, check_gpu=False)
    c.add_paths([str(folder)])

    def wait(cond, timeout=900):
        end = time.time() + timeout
        while time.time() < end:
            s = c.state()
            if s["items"] and cond(s):
                return s
            time.sleep(0.3)
        raise SystemExit("timed out")

    wait(lambda s: all(i["analysis"] in ("done", "skip", "error") for i in s["items"]))

    def tls():
        return {str(i["id"]): c.get_timeline(i["id"]) for i in c.state()["items"]}

    out = {}
    c.set_repair_modes(["interpolate"])
    out["before"] = (c.state(), tls())
    c.set_repair_modes(["off", "interpolate", "cut_stretch"])
    c.set_use_suffix(True)
    out["outputs"] = (c.state(), tls())
    c.start(wait=True)
    out["after"] = (c.state(), tls())
    sel = next(i for i in c.state()["items"] if i["name"] == SELECTED)
    held = next(x[7] for x in CLIPS if x[0] == SELECTED)
    c.set_flags(sel["id"], sorted(held))            # the false detections removed by hand
    out["manual"] = (c.state(), tls())
    c.shutdown()
    return out


MOCK = """
window.pywebview = { api: {
  get_state: () => Promise.resolve(window.__D.state),
  get_timeline: (i) => Promise.resolve(window.__D.tl[String(i)] || null),
  get_video_path: (i) => window.__url(window.__D.names[String(i)]),
  set_flags: () => Promise.resolve(null), reset_flags: () => Promise.resolve(null),
  check_update: () => Promise.resolve({update_available: false}),
}};
"""


def shoot(data, folder, out):
    from playwright.sync_api import sync_playwright

    srv = preview.get_server()
    page_url = (ROOT / "framesnap" / "ui" / "index.html").as_uri()
    out.mkdir(parents=True, exist_ok=True)

    def open_page(browser, key, lang, scheme):
        state, tl = data[key]
        sel = next(i["id"] for i in state["items"] if i["name"] == SELECTED)
        d = {"state": state, "tl": tl, "names": {str(i["id"]): i["name"] for i in state["items"]}}
        ctx = browser.new_context(viewport={"width": 760, "height": 1300}, device_scale_factor=2,
                                  locale="fr-FR" if lang == "fr" else "en-US", color_scheme=scheme)
        pg = ctx.new_page()
        pg.expose_function("__url", lambda name: srv.url_for(str(folder / name)))
        pg.add_init_script("window.__D = %s;" % json.dumps(d) + MOCK)
        pg.goto(page_url)
        pg.evaluate("render(window.__D.state)")
        pg.evaluate("__framesnap.select(%d)" % sel)
        pg.wait_for_function("document.querySelector('#flagList li[data-frame]') !== null")
        pg.wait_for_function("(() => { const v = document.getElementById('preview');"
                             " return v.readyState >= 2 && v.duration > 0; })()")
        pg.click("#flagList li[data-frame='%d'] .flag-seek" % FRAME)
        pg.wait_for_function("(() => { const v = document.getElementById('preview');"
                             " return !v.seeking && v.readyState >= 2; })()")
        pg.evaluate("render(window.__D.state)")              # file names of the selected clip
        pg.evaluate("document.getElementById('preview').dispatchEvent(new Event('seeked'))")
        pg.evaluate("(() => { const li = document.querySelector('#flagList li.hl');"
                    " if (li) li.scrollIntoView({block: 'end'}); })()")
        pg.mouse.move(5, 5)
        pg.wait_for_timeout(1500)
        return ctx, pg

    def union(pg, sels, pad):
        b = [pg.locator(s).bounding_box() for s in sels]
        x0, y0 = min(r["x"] for r in b) - pad, min(r["y"] for r in b) - pad
        x1 = max(r["x"] + r["width"] for r in b) + pad
        y1 = max(r["y"] + r["height"] for r in b) + pad
        return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}

    with sync_playwright() as p:
        exe = os.environ.get("FRAMESNAP_CHROMIUM")
        browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        for lang in ("fr", "en"):
            for scheme, name in (("dark", "main-%s.png"), ("light", "main-%s-light.png")):
                ctx, pg = open_page(browser, "before", lang, scheme)
                pg.screenshot(path=str(out / (name % lang)), full_page=True)
                ctx.close()
            ctx, pg = open_page(browser, "before", lang, "dark")
            pg.evaluate("__framesnap.hover(37.5 / 240)")     # the triple at frame 37
            pg.screenshot(path=str(out / ("timeline-%s.png" % lang)), clip=union(pg, [".tlhead", "#tlbody"], 10))
            ctx.close()
            ctx, pg = open_page(browser, "outputs", lang, "dark")
            pg.screenshot(path=str(out / ("outputs-%s.png" % lang)), clip=union(pg, ["section.repair"], 10))
            ctx.close()
            ctx, pg = open_page(browser, "after", lang, "dark")
            pg.screenshot(path=str(out / ("results-%s.png" % lang)), clip=union(pg, ["#list", "footer"], 8))
            ctx.close()
            ctx, pg = open_page(browser, "manual", lang, "dark")
            pg.locator("#tlbox").screenshot(path=str(out / ("preview-%s.png" % lang)))
            ctx.close()
        browser.close()
    preview.shutdown()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", help="a still frame (png or jpg)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    args = ap.parse_args()
    img = cv2.imdecode(np.fromfile(args.image, np.uint8), cv2.IMREAD_COLOR)   # non ASCII paths too
    if img is None:
        raise SystemExit("cannot read " + args.image)
    ffmpeg = core.find_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="framesnap-shots-") as tmp:
        folder = Path(tmp) / "rushes"
        folder.mkdir()
        for k, (name, n, rate, size, box, zoom, pan, held) in enumerate(CLIPS):
            print("clip", name)
            make_clip(ffmpeg, img, folder / name, n, rate, size, box, zoom, pan, held, k + 1)
        print("analysis and recode (CPU)...")
        data = collect(folder)
        print("screenshots...")
        shoot(data, folder, Path(args.out))
    for f in sorted(Path(args.out).glob("*.png")):
        print(" ", f.name)


if __name__ == "__main__":
    main()
