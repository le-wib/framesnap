"""Command line interface: the same engine as the app, without a window.

    python -m framesnap.cli PATH [PATH ...] [--fps auto|24|25|30|60] [--dry-run]
        [--repair off|interpolate|cut-keep|cut-stretch ...] [--suffix]
        [--sensitivity strict|normal|loose] [--engine auto|gpu|cpu]
    python -m framesnap.cli --gpu-check
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .core import Recoder, collect_jobs, find_ffmpeg


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="framesnap-cli",
        description="Snap clips to a clean constant 24, 25, 30 or 60 fps. "
                    "Outputs go to a 'recoded' folder with the same file names.")
    ap.add_argument("paths", nargs="*", help="video files and/or folders")
    ap.add_argument("--fps", default="auto", choices=["auto", "24", "25", "30", "60"],
                    help="target rate (default: auto, nearest standard rate)")
    ap.add_argument("--overwrite", action="store_true",
                    help="redo files already present in the recoded folder")
    ap.add_argument("--preset", default="slow", help="x264 preset (default: slow)")
    ap.add_argument("--crf", type=int, default=12, help="x264 quality (default: 12)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only analyse and print what would be done")
    ap.add_argument("--repair", action="append", default=[], metavar="MODE",
                    help="what to do with held (duplicated) frames, one output per mode; repeat the "
                         "option or separate modes with commas. off: frame rate only; interpolate: "
                         "new in-between frames, same length; cut-keep: remove them, keep the sound "
                         "(green screen at the end); cut-stretch: remove them, speed the sound up")
    ap.add_argument("--suffix", action="store_true",
                    help="add _recode, _recode-inter, _recode-keep or _recode-speed to the file "
                         "names. Without it, several outputs go to one folder each "
                         "(cadence, inter, keep, speed)")
    ap.add_argument("--sensitivity", default="normal", choices=["strict", "normal", "loose"],
                    help="how readily a frame counts as held (default: normal)")
    ap.add_argument("--engine", default="auto", choices=["auto", "gpu", "cpu"],
                    help="interpolation engine: RIFE on the GPU or optical flow on the CPU")
    ap.add_argument("--gpu-check", action="store_true",
                    help="test whether a GPU can run RIFE, then exit")
    args = ap.parse_args(argv)

    if args.gpu_check:
        from . import rife

        r = rife.gpu_check(force=True)
        print("rife-ncnn-vulkan: %s" % ("found" if r["available"] else "not found"))
        print("device:           %s" % (r["device"] or "none"))
        print("software only:    %s" % r["software"])
        print("test run:         %s (%.2f s)" % ("ok" if r["ok"] else "failed", r["seconds"]))
        if r["error"]:
            print("error:            %s" % r["error"])
        usable = r["ok"] and not r["software"]
        print("GPU interpolation will be %s" % ("USED" if usable else "skipped (CPU optical flow is used)"))
        return 0 if usable else 1
    if not args.paths:
        ap.error("the following arguments are required: paths")

    mode = "auto" if args.fps == "auto" else int(args.fps)
    jobs = collect_jobs(args.paths)
    if not jobs:
        print("No .mp4, .mov or .m4v file found.", file=sys.stderr)
        return 2
    names = {"off": "off", "interpolate": "interpolate", "cut-keep": "cut_keep",
             "cut-stretch": "cut_stretch"}
    wanted = []
    for item in (args.repair or ["off"]):
        for part in item.split(","):
            part = part.strip()
            if part not in names:
                ap.error("unknown --repair mode: %r (choose from %s)" % (part, ", ".join(names)))
            if names[part] not in wanted:
                wanted.append(names[part])
    from .core import VARIANT_ORDER

    wanted = [m for m in VARIANT_ORDER if m in wanted]
    variants = []
    for m in wanted:
        if m == "off":
            variants.append((m, None))
        else:
            from .dedupe import RepairOptions

            if m == "interpolate":
                variants.append((m, RepairOptions("interpolate", sensitivity=args.sensitivity,
                                                  engine=args.engine)))
            else:
                variants.append((m, RepairOptions("cut", "keep" if m == "cut_keep" else "stretch",
                                                  sensitivity=args.sensitivity, engine=args.engine)))
    repair = variants[0][1] if len(variants) == 1 else None
    rec = Recoder(find_ffmpeg(), preset=args.preset, crf=args.crf,
                  skip_existing=not args.overwrite, repair=repair)

    if args.dry_run:
        for job in jobs:
            try:
                probe = rec.probe(job.src)
                plan = rec.plan(probe, mode)
                held = ""
                if wanted != ["off"] and plan.action != "skip":
                    from .dedupe import analyse

                    flagged, diffs = analyse(rec.ffmpeg, job.src, probe, plan, args.sensitivity)
                    held = "  %d held frames (%.1f %%)" % (len(flagged), 100.0 * len(flagged) / max(1, len(diffs) + 1))
                print("%-9s %8.3f fps -> %-4s %s%s" % (
                    plan.action, probe.fps, plan.target or "-", job.rel, held))
            except Exception as exc:  # noqa: BLE001
                print("%-9s %s  %s" % ("error", job.rel, exc))
        return 0

    def on_event(job, kind, info):
        if kind == "result":
            r = info["result"]
            fps = "%.3f" % r.fps if r.fps else "?"
            extra = "  [%s]" % r.backend if r.backend else ""
            out = r.job.dst.relative_to(r.job.out_root.parent)
            print("%-9s %8s fps -> %-4s %s  %s%s" % (
                r.status, fps, r.target or "-", out, r.message, extra), flush=True)

    results = rec.run(jobs, mode, on_event, variants=variants, use_suffix=args.suffix)
    bad = [r for r in results if r.status == "error"]
    print("\n%d done, %d copied, %d skipped, %d existing, %d errors" % (
        sum(r.status == "done" for r in results),
        sum(r.status == "copied" for r in results),
        sum(r.status == "skipped" for r in results),
        sum(r.status == "exists" for r in results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
