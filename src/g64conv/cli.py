"""g64conv command line.

Exit codes (three outcomes, never two):
  0  everything converted and verified
  2  usage error / input missing
  3  conversion or verification failed
  4  converted, but some frames were damaged in the source archive
  5  a required tool is missing (ffmpeg, PyAV)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .format import G64Error
from .tools import MissingDependency

EXIT_OK, EXIT_USAGE, EXIT_FAIL, EXIT_PARTIAL, EXIT_DEPS = 0, 2, 3, 4, 5


class Out:
    def __init__(self, quiet: bool, color: bool) -> None:
        self.quiet = quiet; self.color = color

    def say(self, s: str) -> None:
        if not self.quiet:
            print(s, flush=True)

    def status(self, tag: str, s: str) -> None:
        colors = {"OK": "32", "PARTIAL": "33", "FAILED": "31", "VERIFY-FAIL": "31", "MISSING": "31", "DRY-RUN": "36"}
        t = f"\033[{colors.get(tag, '0')}m{tag:11}\033[0m" if self.color else f"{tag:11}"
        self.say(f"{t} {s}")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="print a JSON report to stdout (human output goes to stderr)")
    p.add_argument("--quiet", action="store_true", help="no progress output")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="show what would be done, write nothing")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="g64conv", description="Convert Genetec .g64/.g64x video archives natively.")
    ap.add_argument("--version", action="version", version=f"g64conv {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="archive(s) -> MP4 (bit-exact copy), optionally other formats / dewarped views")
    c.add_argument("inputs", nargs="+")
    c.add_argument("-o", "--out-dir", default=".", help="output directory (default: current directory)")
    c.add_argument("--format", action="append", default=None, choices=["mp4", "mkv", "mov", "webm", "gif", "frames", "hls"],
                   help="output format(s); repeatable. mp4 is always produced first")
    c.add_argument("--dewarp", choices=["none", "double", "panorama"], default="none",
                   help="for circular fisheye cameras: split into two normal views (double) or one 360 strip")
    c.add_argument("--fisheye-fov", type=float, default=180.0, help="lens field of view of the fisheye circle")
    c.add_argument("--mount", choices=["auto", "ceiling", "wall"], default="auto",
                   help="how the fisheye is mounted; auto measures a frame (a black upper half = wall)")
    c.add_argument("--keep-h264", action="store_true", help="also write the raw Annex-B elementary stream")
    c.add_argument("--no-verify", action="store_true")
    c.add_argument("--report", help="write the JSON report to this file as well")
    _common(c)

    d = sub.add_parser("dewarp", help="dewarp any fisheye video (e.g. an MP4 already converted)")
    d.add_argument("inputs", nargs="+")
    d.add_argument("-o", "--out-dir", default=".")
    d.add_argument("--mode", choices=["double", "panorama"], default="double")
    d.add_argument("--mount", choices=["auto", "ceiling", "wall"], default="auto",
                   help="how the fisheye is mounted; auto measures a frame (a black upper half = wall)")
    d.add_argument("--fov", type=float, default=180.0)
    d.add_argument("--width", type=int, default=0, help="output width per view (default: 2x input for double, 4x for panorama)")
    d.add_argument("--above-horizon", type=float, default=15.0, help="degrees kept above the rim/horizon")
    d.add_argument("--crf", type=int, default=18)
    _common(d)

    t = sub.add_parser("transcode", help="MP4 -> mkv/mov/webm/gif/frames/hls with ffmpeg")
    t.add_argument("inputs", nargs="+")
    t.add_argument("-o", "--out-dir", default=None)
    t.add_argument("--format", action="append", required=True, choices=["mkv", "mov", "webm", "gif", "frames", "hls"])
    t.add_argument("--fps", type=float, help="resample frame rate (webm/gif/frames)")
    t.add_argument("--width", type=int, help="scale to this width (webm/gif/frames)")
    _common(t)

    pr = sub.add_parser("probe", help="inspect an archive's header and frames")
    pr.add_argument("input")
    pr.add_argument("--max-frames", type=int)
    _common(pr)

    w = sub.add_parser("synth", help="write a synthetic .g64/.g64x archive from any video (test fixtures)")
    w.add_argument("video")
    w.add_argument("output", help="path ending in .g64 or .g64x")
    w.add_argument("--segment-seconds", type=float, help="split into _N continuation segments")
    w.add_argument("--collection", default="synthetic")
    _common(w)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    out = Out(a.quiet or a.json, color=(not a.no_color) and sys.stdout.isatty())
    if a.json:
        out_stream = sys.stderr
        out.say = lambda s: (None if out.quiet else print(s, file=out_stream, flush=True))  # type: ignore[method-assign]
    try:
        return {"convert": cmd_convert, "dewarp": cmd_dewarp, "transcode": cmd_transcode,
                "probe": cmd_probe, "synth": cmd_synth}[a.cmd](a, out)
    except MissingDependency as e:
        print(f"missing dependency: {e}", file=sys.stderr)
        return EXIT_DEPS
    except ImportError as e:
        print(f"missing dependency: {e} (pip install av)", file=sys.stderr)
        return EXIT_DEPS


def _emit(a, report) -> None:
    if a.json:
        print(json.dumps(report, indent=2, default=str))
    if getattr(a, "report", None):
        with open(a.report, "w") as fh:
            json.dump(report, fh, indent=2, default=str)


def cmd_convert(a, out: Out) -> int:
    from .archive import discover
    from .convert import convert_source, verify
    from .dewarp import DewarpSpec, dewarp
    from .transcode import transcode

    worst = EXIT_OK; report = []
    formats = [f for f in (a.format or []) if f != "mp4"]
    for inp in a.inputs:
        if not os.path.isfile(inp):
            out.status("MISSING", inp); worst = max(worst, EXIT_USAGE); continue
        try:
            sources = discover(inp)
        except (G64Error, OSError) as e:
            out.status("FAILED", f"{inp}: {e}"); worst = max(worst, EXIT_FAIL); continue
        out.say(f"== {inp}: {len(sources)} video source(s)")
        for src in sources:
            out.say(f"-- {src.label} ({len(src.segments)} segment(s))")
            if a.dry_run:
                out.status("DRY-RUN", f"would write {src.label} -> {a.out_dir} formats={['mp4'] + formats} dewarp={a.dewarp}")
                continue
            os.makedirs(a.out_dir, exist_ok=True)
            try:
                r = convert_source(src, a.out_dir, keep_h264=a.keep_h264, log=out.say)
            except G64Error as e:
                out.status("FAILED", f"{src.label}: {e}"); worst = max(worst, EXIT_FAIL); continue
            status = "OK"
            if not a.no_verify and not verify(r):
                status = "VERIFY-FAIL"; worst = max(worst, EXIT_FAIL)
                out.status(status, f"{r.output}: {r.verify}")
            if r.frames_damaged and status == "OK":
                status = "PARTIAL"; worst = max(worst, EXIT_PARTIAL)
            r.status = status
            d = r.to_dict()
            d["extra_outputs"] = {}
            if status != "VERIFY-FAIL":
                for fmt in formats:
                    try:
                        d["extra_outputs"][fmt] = transcode(r.output, fmt, a.out_dir)
                    except RuntimeError as e:
                        out.status("FAILED", f"{fmt}: {e}"); worst = max(worst, EXIT_FAIL)
                if a.dewarp != "none":
                    try:
                        d["extra_outputs"]["dewarp"] = dewarp(r.output, a.out_dir, DewarpSpec(mode=a.dewarp, mount=a.mount, fov=a.fisheye_fov))
                    except RuntimeError as e:
                        out.status("FAILED", f"dewarp: {e}"); worst = max(worst, EXIT_FAIL)
            report.append(d)
            out.status(status, f"{r.output}  [{r.width}x{r.height} h264, {r.frames_written} video frames of "
                       f"{r.frames_in_archive} ({r.frames_non_video} metadata-only, {r.frames_damaged} damaged), "
                       f"{r.duration_s}s, rotation {r.genetec_rotation_deg}, start {r.start_utc}]")
            for k, v in d["extra_outputs"].items():
                out.say(f"           {k}: {v}")
    _emit(a, report)
    return worst


def cmd_dewarp(a, out: Out) -> int:
    from .dewarp import DewarpSpec, dewarp

    worst = EXIT_OK; report = []
    spec = DewarpSpec(mode=a.mode, mount=a.mount, fov=a.fov, width=a.width, above_horizon_deg=a.above_horizon, crf=a.crf)
    for inp in a.inputs:
        if not os.path.isfile(inp):
            out.status("MISSING", inp); worst = max(worst, EXIT_USAGE); continue
        if a.dry_run:
            out.status("DRY-RUN", f"would dewarp {inp} mode={a.mode} mount={a.mount} fov={a.fov:g} -> {a.out_dir}"); continue
        try:
            outs = dewarp(inp, a.out_dir, spec)
        except RuntimeError as e:
            out.status("FAILED", f"{inp}: {e}"); worst = max(worst, EXIT_FAIL); continue
        report.append({"input": inp, "outputs": outs, "mode": a.mode, "mount": a.mount, "fov": a.fov})
        for o in outs:
            out.status("OK", o)
    _emit(a, report)
    return worst


def cmd_transcode(a, out: Out) -> int:
    from .transcode import transcode

    worst = EXIT_OK; report = []
    for inp in a.inputs:
        if not os.path.isfile(inp):
            out.status("MISSING", inp); worst = max(worst, EXIT_USAGE); continue
        for fmt in a.format:
            if a.dry_run:
                out.status("DRY-RUN", f"would write {fmt} of {inp}"); continue
            try:
                o = transcode(inp, fmt, a.out_dir, fps=a.fps, scale_width=a.width)
            except RuntimeError as e:
                out.status("FAILED", f"{inp} {fmt}: {e}"); worst = max(worst, EXIT_FAIL); continue
            report.append({"input": inp, "format": fmt, "output": o}); out.status("OK", o)
    _emit(a, report)
    return worst


def cmd_probe(a, out: Out) -> int:
    from .probe import probe

    if not os.path.isfile(a.input):
        out.status("MISSING", a.input); return EXIT_USAGE
    try:
        rep = probe(a.input, a.max_frames)
    except G64Error as e:
        out.status("FAILED", str(e)); return EXIT_FAIL
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        for d in rep:
            h = d["header"]
            out.say(f"{d['segment']}: {h['startcode']} source={d['source']!r} frames={d['frames']} "
                    f"watermarked={h['watermarked']} encrypted={h['frames_encrypted']}")
            out.say(f"   compression={d['compression_types']} rtp_pt={d['rtp_payload_types']} nal={d['nal_units']}")
            if "frame_interval_ms" in d:
                out.say(f"   start={d['start_utc']} span={d['span_s']}s interval_ms={d['frame_interval_ms']} rotation={d['rotation_messages']}")
    return EXIT_OK


def cmd_synth(a, out: Out) -> int:
    from .writer import write_g64, write_g64x

    if not os.path.isfile(a.video):
        out.status("MISSING", a.video); return EXIT_USAGE
    if a.dry_run:
        out.status("DRY-RUN", f"would write {a.output} from {a.video}"); return EXIT_OK
    try:
        if a.output.lower().endswith(".g64x"):
            paths = [write_g64x(a.video, a.output, collection=a.collection, segment_seconds=a.segment_seconds)]
        elif a.output.lower().endswith(".g64"):
            paths = write_g64(a.video, a.output, collection=a.collection, segment_seconds=a.segment_seconds)
        else:
            out.status("FAILED", "output must end in .g64 or .g64x"); return EXIT_USAGE
    except RuntimeError as e:
        out.status("FAILED", str(e)); return EXIT_FAIL
    for p in paths:
        out.status("OK", p)
    _emit(a, {"outputs": paths})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
