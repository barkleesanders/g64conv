"""Local web GUI: ``g64conv gui``.

A single page served from the Python standard library on 127.0.0.1. Drop
``.g64`` / ``.g64x`` archives (or name a path already on this computer),
watch each conversion in a lab-notebook log, then play the verified MP4s in
the browser and produce the other formats or dewarped views from them.

Nothing leaves the machine: no CDN, no fonts, no analytics, and the server
only listens on the loopback interface.

Safety model (this is a local tool, but a browser is a shared space):
- Bound to 127.0.0.1 only.
- The ``Host`` header must name the loopback address (DNS-rebinding guard).
- Every state-changing request must carry the ``X-G64conv: 1`` header. That
  header makes the request "non-simple", so a web page from another origin
  cannot send it without a CORS preflight, which this server never grants.
- ``/files/`` only serves paths inside the output directory (no traversal),
  and the API only reads inputs that already exist as files; nothing is ever
  deleted.
"""

from __future__ import annotations

import datetime
import json
import mimetypes
import os
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import __version__
from .format import G64Error
from .tools import MissingDependency, ffprobe_json

ARCHIVE_EXTS = (".g64", ".g64x")
OUTPUT_FILE_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".gif")
OUTPUT_DIR_SUFFIXES = ("_frames", "_hls")
API_HEADER = "X-G64conv"


# --------------------------------------------------------------------------- jobs

@dataclass
class Job:
    id: str
    kind: str                      # convert | dewarp | transcode
    inputs: list[str]
    options: dict
    status: str = "queued"         # queued | running | ok | partial | failed
    progress: float = 0.0          # 0..1, -1 = unknown
    message: str = ""
    log: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    finished: float | None = None

    def note(self, s: str) -> None:
        self.log.append(f"{datetime.datetime.now().astimezone().strftime('%H:%M:%S')}  {s}")

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "inputs": self.inputs, "options": self.options,
                "status": self.status, "progress": self.progress, "message": self.message,
                "log": self.log, "results": self.results, "created": self.created, "finished": self.finished}


class Runner:
    """One worker thread; ffmpeg saturates the machine, so jobs run one at a time."""

    def __init__(self, out_dir: str) -> None:
        self.out_dir = out_dir
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.q: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, name="g64conv-runner", daemon=True)
        self.thread.start()

    def submit(self, kind: str, inputs: list[str], options: dict) -> Job:
        job = Job(uuid.uuid4().hex[:8], kind, inputs, options)
        with self.lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
        job.note(f"queued {kind} of {len(inputs)} input(s)")
        self.q.put(job.id)
        return job

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [self.jobs[i].to_dict() for i in reversed(self.order)]

    def _loop(self) -> None:
        while True:
            jid = self.q.get()
            job = self.jobs[jid]
            job.status = "running"
            try:
                {"convert": self._convert, "dewarp": self._dewarp, "transcode": self._transcode}[job.kind](job)
                if job.status == "running":
                    job.status = "ok"
            except (G64Error, MissingDependency, RuntimeError, ValueError, OSError) as e:
                job.status = "failed"
                job.message = str(e)
                job.note(f"FAILED {e}")
            except Exception as e:  # keep the server alive, report the surprise honestly
                job.status = "failed"
                job.message = f"{type(e).__name__}: {e}"
                job.note(f"FAILED {job.message}")
            finally:
                job.finished = time.time()
                if job.progress >= 0:
                    job.progress = 1.0

    # -- kinds
    def _convert(self, job: Job) -> None:
        from .archive import discover
        from .convert import convert_source, verify
        from .dewarp import DewarpSpec, dewarp
        from .transcode import transcode

        formats = [f for f in job.options.get("formats", []) if f != "mp4"]
        dw = job.options.get("dewarp", "none")
        mount = job.options.get("mount", "auto")
        sources = []
        for inp in job.inputs:
            srcs = discover(inp)
            job.note(f"{os.path.basename(inp)}: {len(srcs)} video source(s)")
            sources += [(inp, s) for s in srcs]
        n = max(len(sources), 1)
        worst = "ok"
        for i, (_inp, src) in enumerate(sources):
            base = i / n
            job.message = f"converting {src.label}"
            job.note(f"-- {src.label} ({len(src.segments)} segment(s))")

            def seg_progress(done: int, total: int, base=base) -> None:
                job.progress = base + (done / max(total, 1)) * 0.7 / n

            r = convert_source(src, self.out_dir, log=job.note, progress=seg_progress)
            job.message = f"verifying {os.path.basename(r.output)}"
            job.progress = base + 0.75 / n
            ok = verify(r)
            r.status = "OK" if ok else "VERIFY-FAIL"
            if ok and r.frames_damaged:
                r.status = "PARTIAL"
            d = r.to_dict()
            d["extra_outputs"] = {}
            v = r.verify
            job.note(f"{r.status} {os.path.basename(r.output)}: {r.frames_written} written, "
                     f"{v.get('mp4_decoded_frames', '?')} decoded, {r.frames_damaged} damaged, rotation {r.genetec_rotation_deg}")
            if r.status == "VERIFY-FAIL":
                worst = "failed"
                job.note(f"   verify detail: {v}")
            elif r.status == "PARTIAL" and worst == "ok":
                worst = "partial"
            if r.status != "VERIFY-FAIL":
                extras = len(formats) + (1 if dw != "none" else 0)

                def extra_progress(k: int, base: float = base, extras: int = extras):
                    def cb(f: float) -> None:
                        job.progress = base + (0.75 + 0.25 * (k + max(f, 0.0)) / extras) / n
                    return cb
                for k, fmt in enumerate(formats):
                    job.message = f"{fmt} of {os.path.basename(r.output)}"
                    d["extra_outputs"][fmt] = transcode(r.output, fmt, self.out_dir, progress=extra_progress(k))
                    job.note(f"   {fmt}: {os.path.basename(d['extra_outputs'][fmt])}")
                if dw != "none":
                    job.message = f"dewarp of {os.path.basename(r.output)}"
                    outs = dewarp(r.output, self.out_dir, DewarpSpec(mode=dw, mount=mount),
                                  progress=extra_progress(len(formats)))
                    d["extra_outputs"]["dewarp"] = outs
                    job.note("   dewarp: " + ", ".join(os.path.basename(o) for o in outs))
            job.results.append(d)
        job.status = worst
        job.message = {"ok": "all outputs verified", "partial": "converted; some source frames were damaged",
                       "failed": "verification failed"}[worst]

    def _dewarp(self, job: Job) -> None:
        from .dewarp import DewarpSpec, detect_mount, dewarp

        for inp in job.inputs:
            spec = DewarpSpec(mode=job.options.get("mode", "double"), mount=job.options.get("mount", "auto"))
            mount = detect_mount(inp) if spec.mount == "auto" else spec.mount
            job.note(f"{os.path.basename(inp)}: mount {mount} ({'measured' if spec.mount == 'auto' else 'forced'}), mode {spec.mode}")
            job.message = f"dewarping {os.path.basename(inp)}"
            spec.mount = mount
            outs = dewarp(inp, self.out_dir, spec, progress=lambda f: setattr(job, "progress", f))
            job.results.append({"input": inp, "outputs": outs, "mount": mount, "mode": spec.mode})
            job.note("wrote " + ", ".join(os.path.basename(o) for o in outs))
        job.message = "dewarp finished"

    def _transcode(self, job: Job) -> None:
        from .transcode import transcode

        fmts = job.options.get("formats", [])
        total = max(len(job.inputs) * len(fmts), 1)
        k = 0

        def step_progress(k: int):
            def cb(f: float) -> None:
                job.progress = (k + max(f, 0.0)) / total
            return cb
        for inp in job.inputs:
            for fmt in fmts:
                job.message = f"{fmt} of {os.path.basename(inp)}"
                out = transcode(inp, fmt, self.out_dir, progress=step_progress(k))
                job.results.append({"input": inp, "format": fmt, "output": out})
                job.note(f"{fmt}: {os.path.basename(out)}")
                k += 1
        job.message = "transcode finished"


# --------------------------------------------------------------------------- outputs

class Outputs:
    """What is in the output directory, with ffprobe facts cached per (path, size, mtime)."""

    def __init__(self, out_dir: str) -> None:
        self.out_dir = out_dir
        self.cache: dict[tuple, dict] = {}

    SKIP_DIRS = ("uploads",)
    MAX_DEPTH = 3

    def list(self) -> list[dict]:
        """Videos in the output directory and its subfolders (e.g. a dewarped/ folder
        made by an earlier run), named by their path relative to the output directory."""
        items = []
        for rel_dir, _depth in self._walk():
            full_dir = os.path.join(self.out_dir, rel_dir) if rel_dir else self.out_dir
            try:
                names = sorted(os.listdir(full_dir))
            except OSError:
                continue
            for name in names:
                p = os.path.join(full_dir, name)
                # API output names use URL-style separators on every platform.
                rel = (os.path.join(rel_dir, name) if rel_dir else name).replace(os.sep, "/")
                url = "/files/" + "/".join(quote(seg, safe="") for seg in rel.split("/"))
                low = name.lower()
                if os.path.isfile(p) and low.endswith(OUTPUT_FILE_EXTS):
                    st = os.stat(p)
                    key = (p, st.st_size, int(st.st_mtime))
                    if key not in self.cache:
                        self.cache[key] = self._probe(p)
                    items.append({"name": rel, "kind": "file", "size": st.st_size, "mtime": st.st_mtime,
                                  "url": url, "playable": low.endswith((".mp4", ".webm", ".mov")),
                                  **self.cache[key]})
                elif os.path.isdir(p) and low.endswith(OUTPUT_DIR_SUFFIXES):
                    try:
                        count = len(os.listdir(p))
                    except OSError:
                        count = 0
                    items.append({"name": rel, "kind": "dir", "entries": count, "mtime": os.stat(p).st_mtime,
                                  "url": url + "/"})
        items.sort(key=lambda d: d["mtime"], reverse=True)
        return items

    def _walk(self):
        """(relative_dir, depth) pairs, breadth-first, skipping uploads/, hidden dirs,
        frames/hls output dirs, and symlinks; bounded so a huge tree cannot stall the page."""
        pending = [("", 0)]
        while pending:
            rel, depth = pending.pop(0)
            yield rel, depth
            if depth >= self.MAX_DEPTH:
                continue
            full = os.path.join(self.out_dir, rel) if rel else self.out_dir
            try:
                for name in sorted(os.listdir(full)):
                    p = os.path.join(full, name)
                    low = name.lower()
                    if (name.startswith(".") or name in self.SKIP_DIRS or low.endswith(OUTPUT_DIR_SUFFIXES)
                            or os.path.islink(p) or not os.path.isdir(p)):
                        continue
                    pending.append((os.path.join(rel, name) if rel else name, depth + 1))
            except OSError:
                continue

    @staticmethod
    def _probe(p: str) -> dict:
        try:
            j = ffprobe_json(p, "stream=codec_name,width,height,nb_frames", "stream_side_data_list", "format=duration")
            st = (j.get("streams") or [{}])[0]
            rot = None
            for sd in st.get("side_data_list", []) or []:
                if "rotation" in sd:
                    rot = sd["rotation"]
            return {"codec": st.get("codec_name"), "width": st.get("width"), "height": st.get("height"),
                    "frames": int(st["nb_frames"]) if st.get("nb_frames", "").isdigit() else None,
                    "duration": float(j.get("format", {}).get("duration", 0) or 0), "rotation": rot}
        except (RuntimeError, MissingDependency, ValueError, KeyError):
            return {"codec": None, "width": None, "height": None, "frames": None, "duration": None, "rotation": None}


# --------------------------------------------------------------------------- http

def _safe_name(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "upload"
    return name[:120]


class App:
    def __init__(self, out_dir: str) -> None:
        self.out_dir = os.path.abspath(out_dir)
        self.upload_dir = os.path.join(self.out_dir, "uploads")
        os.makedirs(self.upload_dir, exist_ok=True)
        self.runner = Runner(self.out_dir)
        self.outputs = Outputs(self.out_dir)
        self.started = time.time()

    def state(self) -> dict:
        # One snapshot for both the list and the busy flag: two snapshots let a job finish
        # in between, so a client saw busy=false next to a job still marked running (CI, 2026-09).
        jobs = self.runner.snapshot()
        return {"version": __version__, "out_dir": self.out_dir, "upload_dir": self.upload_dir,
                "tools": {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))},
                "jobs": jobs, "outputs": self.outputs.list(),
                "busy": any(j["status"] in ("queued", "running") for j in jobs)}


class Handler(BaseHTTPRequestHandler):
    app: App  # set by make_server
    server_version = f"g64conv/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; the page has its own log
        pass

    # ---- guards
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _mutation_ok(self) -> bool:
        if self.headers.get(API_HEADER) != "1":
            return False
        origin = self.headers.get("Origin")
        if origin:
            h = urlsplit(origin).hostname or ""
            return h.lower() in ("127.0.0.1", "localhost", "::1")
        return True

    # ---- replies
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode())

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    def _content_length(self) -> int:
        raw = self.headers.get("Content-Length") or "0"
        try:
            n = int(raw)
        except ValueError:
            raise ValueError(f"bad Content-Length header: {raw!r}") from None
        if n < 0:
            raise ValueError("negative Content-Length")
        return n

    def _read_json(self) -> dict:
        n = self._content_length()
        if n > 1_000_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(n) if n else b"{}"
        obj = json.loads(raw or b"{}")
        if not isinstance(obj, dict):
            raise TypeError("expected a JSON object")
        return obj

    # ---- routing
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._err(403, "host not allowed")
        u = urlsplit(self.path)
        if u.path == "/":
            return self._send(200, PAGE.replace("__VERSION__", __version__).encode(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'none'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'unsafe-inline'; media-src 'self'; img-src 'self' data:; connect-src 'self'"})
        if u.path == "/api/state":
            return self._json(200, self.app.state())
        if u.path.startswith("/files/"):
            return self._serve_file(unquote(u.path[len("/files/"):]))
        self._err(404, "not found")

    def do_PUT(self) -> None:
        if not self._host_ok():
            return self._err(403, "host not allowed")
        if not self._mutation_ok():
            return self._err(403, f"missing {API_HEADER} header")
        u = urlsplit(self.path)
        if u.path != "/api/upload":
            return self._err(404, "not found")
        name = _safe_name(parse_qs(u.query).get("name", ["upload"])[0])
        if not name.lower().endswith(ARCHIVE_EXTS):
            return self._err(400, "only .g64 and .g64x files are accepted")
        try:
            n = self._content_length()
        except ValueError as e:
            return self._err(400, str(e))
        dest = os.path.join(self.app.upload_dir, name)
        stem, ext = os.path.splitext(dest)
        k = 1
        while os.path.exists(dest):
            dest = f"{stem}_{k}{ext}"
            k += 1
        remaining = n
        with open(dest, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining:
            os.remove(dest)
            return self._err(400, "upload truncated")
        self._json(200, {"path": dest, "size": n})

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._err(403, "host not allowed")
        if not self._mutation_ok():
            return self._err(403, f"missing {API_HEADER} header")
        u = urlsplit(self.path)
        try:
            body = self._read_json()
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            return self._err(400, f"bad JSON: {e}")
        try:
            if u.path == "/api/convert":
                paths = self._existing(body.get("paths"), ARCHIVE_EXTS)
                formats = [f for f in body.get("formats", []) if f in ("mkv", "mov", "webm", "gif", "frames", "hls")]
                dw = body.get("dewarp", "none")
                if dw not in ("none", "double", "panorama"):
                    raise ValueError("dewarp must be none, double or panorama")
                mount = body.get("mount", "auto")
                if mount not in ("auto", "ceiling", "wall"):
                    raise ValueError("mount must be auto, ceiling or wall")
                job = self.app.runner.submit("convert", paths, {"formats": formats, "dewarp": dw, "mount": mount})
            elif u.path == "/api/dewarp":
                paths = self._existing(body.get("paths"), (".mp4",))
                mode = body.get("mode", "double")
                mount = body.get("mount", "auto")
                if mode not in ("double", "panorama") or mount not in ("auto", "ceiling", "wall"):
                    raise ValueError("bad mode/mount")
                job = self.app.runner.submit("dewarp", paths, {"mode": mode, "mount": mount})
            elif u.path == "/api/transcode":
                paths = self._existing(body.get("paths"), (".mp4",))
                fmts = [f for f in body.get("formats", []) if f in ("mkv", "mov", "webm", "gif", "frames", "hls")]
                if not fmts:
                    raise ValueError("no formats given")
                job = self.app.runner.submit("transcode", paths, {"formats": fmts})
            else:
                return self._err(404, "not found")
        except (ValueError, TypeError) as e:
            return self._err(400, str(e))
        self._json(200, {"job": job.to_dict()})

    def _existing(self, paths, exts: tuple) -> list[str]:
        if not isinstance(paths, list) or not paths:
            raise ValueError("paths must be a non-empty list")
        out = []
        for raw in paths:
            if not isinstance(raw, str):
                raise TypeError("paths must be strings")
            p = os.path.expanduser(raw)
            if not os.path.isfile(p):
                raise ValueError(f"not a file: {p}")
            if not p.lower().endswith(exts):
                raise ValueError(f"{os.path.basename(p)}: expected one of {', '.join(exts)}")
            out.append(os.path.abspath(p))
        return out

    # ---- output files with HTTP Range (the <video> element seeks with it)
    def _serve_file(self, rel: str) -> None:
        root = os.path.realpath(self.app.out_dir)
        target = os.path.realpath(os.path.join(root, rel))
        if target != root and not target.startswith(root + os.sep):
            return self._err(404, "not found")
        if os.path.isdir(target):
            try:
                names = sorted(os.listdir(target))
            except OSError:
                return self._err(404, "not found")
            body = json.dumps({"dir": os.path.relpath(target, root), "entries": names}).encode()
            return self._send(200, body)
        if not os.path.isfile(target):
            return self._err(404, "not found")
        size = os.path.getsize(target)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if target.endswith(".m3u8"):
            ctype = "application/vnd.apple.mpegurl"
        elif target.endswith(".ts"):
            ctype = "video/mp2t"
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m:
                a, b = m.group(1), m.group(2)
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:
                    start = max(size - int(b), 0)
                if start >= size or end < start:
                    return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                end = min(end, size - 1)
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(target, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)


def make_server(out_dir: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    app = App(out_dir)
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(out_dir: str, port: int = 8765, open_browser: bool = True, log=print) -> int:
    try:
        srv = make_server(out_dir, port=port)
    except OSError as e:
        log(f"could not listen on 127.0.0.1:{port}: {e}")
        return 3
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    log(f"g64conv {__version__} gui at {url}  (outputs -> {os.path.abspath(out_dir)}; Ctrl-C to stop)")
    if not shutil.which("ffmpeg"):
        log("WARNING: ffmpeg not found on PATH; conversions will fail until it is installed")
    if open_browser:
        import webbrowser
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


# --------------------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>g64conv</title>
<style>
/* Hallmark · macrostructure: Workbench · tone: brutalist instrument · anchor hue: vermillion 35
 * theme: custom (vibe: "evidence bench, lab notebook, hard edges" · paper oklch(96% 0.006 80) ·
 * accent oklch(52% 0.19 35) warm · system monospace only, no network fonts) · nav: N8 terminal status
 * line · footer: none · enrichment: none · pre-emit critique: P4 H4 E4 S4 R5 V4 */
:root {
  --color-paper: oklch(96% 0.006 80);
  --color-paper-2: oklch(92% 0.008 80);
  --color-paper-3: oklch(86% 0.01 80);
  --color-ink: oklch(20% 0.01 80);
  --color-ink-2: oklch(42% 0.012 80);
  --color-rule: oklch(30% 0.01 80);
  --color-accent: oklch(52% 0.19 35);
  --color-accent-ink: oklch(99% 0 0);
  --color-focus: oklch(45% 0.2 250);
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --text-xs: 0.75rem; --text-sm: 0.8125rem; --text-md: 0.9375rem; --text-lg: 1.25rem; --text-xl: 1.75rem;
  --space-xs: 4px; --space-sm: 8px; --space-md: 16px; --space-lg: 24px; --space-xl: 40px;
  --rule: 2px solid var(--color-rule);
  --rule-thin: 1px solid var(--color-paper-3);
  --ease-out: cubic-bezier(0.2, 0.8, 0.2, 1);
  --dur-fast: 120ms;
}
* { box-sizing: border-box; }
html, body { overflow-x: clip; }
body { margin: 0; background: var(--color-paper); color: var(--color-ink); font: var(--text-md)/1.45 var(--font-mono); }
a { color: var(--color-ink); }
h1, h2, h3 { font-weight: 700; font-style: normal; margin: 0; letter-spacing: -0.01em; overflow-wrap: anywhere; min-width: 0; }
h1 { font-size: var(--text-xl); }
h2 { font-size: var(--text-lg); border-bottom: var(--rule); padding-bottom: var(--space-xs); margin-bottom: var(--space-md); }
.status { display: flex; flex-wrap: wrap; gap: var(--space-md); align-items: baseline; padding: var(--space-sm) var(--space-md);
  border-bottom: var(--rule); font-size: var(--text-sm); }
.status .v { color: var(--color-ink-2); }
.status .bad { color: var(--color-accent); font-weight: 700; }
main { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: var(--space-lg); padding: var(--space-lg) var(--space-md); }
@media (max-width: 900px) { main { grid-template-columns: minmax(0, 1fr); } }
section { min-width: 0; }
.drop { border: var(--rule); padding: var(--space-xl) var(--space-md); text-align: center; cursor: pointer;
  transition: background var(--dur-fast) var(--ease-out); }
.drop.over, .drop:hover { background: var(--color-paper-2); }
.drop:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, a:focus-visible, video:focus-visible {
  outline: 3px solid var(--color-focus); outline-offset: 2px; }
.drop strong { display: block; font-size: var(--text-lg); }
.drop input { display: none; }
.row { display: flex; flex-wrap: wrap; gap: var(--space-sm); align-items: center; margin-top: var(--space-md); }
.row label { font-size: var(--text-sm); color: var(--color-ink-2); }
input[type=text], select { font: inherit; padding: var(--space-xs) var(--space-sm); border: var(--rule); background: var(--color-paper);
  color: var(--color-ink); min-width: 0; }
input[type=text] { flex: 1 1 240px; }
button { font: inherit; font-weight: 700; padding: var(--space-xs) var(--space-md); border: var(--rule); background: var(--color-paper);
  color: var(--color-ink); cursor: pointer; white-space: nowrap; transition: background var(--dur-fast) var(--ease-out), transform var(--dur-fast) var(--ease-out); }
button:hover { background: var(--color-paper-2); }
button:active { transform: translateY(1px); }
button.primary { background: var(--color-accent); color: var(--color-accent-ink); border-color: var(--color-accent); }
button.primary:hover { background: oklch(46% 0.19 35); }
button[disabled] { opacity: 0.45; cursor: not-allowed; transform: none; }
.opts { display: flex; flex-wrap: wrap; gap: var(--space-sm) var(--space-md); margin-top: var(--space-md); font-size: var(--text-sm); }
.opts label { display: inline-flex; gap: var(--space-xs); align-items: center; }
.jobs { margin-top: var(--space-lg); display: grid; gap: var(--space-sm); }
.job { border: var(--rule-thin); padding: var(--space-sm) var(--space-md); }
.job header { display: flex; justify-content: space-between; gap: var(--space-sm); font-size: var(--text-sm); }
.job .bar { height: 6px; background: var(--color-paper-3); margin-top: var(--space-xs); }
.job .bar i { display: block; height: 100%; background: var(--color-ink); transition: width 300ms var(--ease-out); }
.job.running .bar i { background: var(--color-accent); }
.job.failed header b, .job.partial header b { color: var(--color-accent); }
.job.unknown .bar i { width: 30% !important; animation: slide 1.2s linear infinite; }
@keyframes slide { from { margin-left: 0 } to { margin-left: 70% } }
.tag { border: 1px solid currentColor; padding: 0 var(--space-xs); font-size: var(--text-xs); }
.log { border: var(--rule); margin-top: var(--space-lg); padding: var(--space-sm) var(--space-md); max-height: 40vh; overflow: auto;
  font-size: var(--text-sm); white-space: pre-wrap; overflow-wrap: anywhere; background: var(--color-paper-2); }
.outs { display: grid; gap: var(--space-sm); }
.out { border: var(--rule-thin); padding: var(--space-sm) var(--space-md); display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--space-sm); align-items: start; }
.out .name { font-weight: 700; overflow-wrap: anywhere; }
.out .facts { font-size: var(--text-sm); color: var(--color-ink-2); }
.out .acts { display: flex; flex-wrap: wrap; gap: var(--space-xs); justify-content: flex-end; }
.out.playing { border: var(--rule); }
.player { margin-bottom: var(--space-md); }
.player video { width: 100%; max-height: 60vh; background: var(--color-ink); display: block; }
.player .cap { font-size: var(--text-sm); color: var(--color-ink-2); margin-top: var(--space-xs); overflow-wrap: anywhere; }
.empty { color: var(--color-ink-2); font-size: var(--text-sm); }
.sr { position: absolute; left: -9999px; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
</style>
</head>
<body>
<header class="status" role="status">
  <h1>g64conv <span class="v">__VERSION__</span></h1>
  <span id="st-tools" class="v">checking ffmpeg…</span>
  <span id="st-out" class="v"></span>
  <span id="st-busy" class="v"></span>
</header>
<main>
  <section>
    <h2>Archives</h2>
    <div class="drop" id="drop" tabindex="0" role="button" aria-label="Drop .g64 or .g64x archives here, or press to choose files">
      <strong>Drop .g64 / .g64x here</strong>
      <span class="v">or press to choose files</span>
      <input type="file" id="file" multiple accept=".g64,.g64x">
    </div>
    <div class="row">
      <label for="path">Already on this computer</label>
      <input type="text" id="path" placeholder="/Volumes/USB/export.g64x" spellcheck="false">
      <button id="convert-path">Convert path</button>
    </div>
    <div class="opts" id="opts">
      <span>Also produce:</span>
      <label><input type="checkbox" name="fmt" value="mkv"> mkv</label>
      <label><input type="checkbox" name="fmt" value="mov"> mov</label>
      <label><input type="checkbox" name="fmt" value="webm"> webm</label>
      <label><input type="checkbox" name="fmt" value="gif"> gif</label>
      <label><input type="checkbox" name="fmt" value="frames"> png frames</label>
      <label><input type="checkbox" name="fmt" value="hls"> hls</label>
      <label>Fisheye <select id="dewarp"><option value="none">no dewarp</option><option value="double">two views</option><option value="panorama">panorama</option></select></label>
      <label>Mount <select id="mount"><option value="auto">auto</option><option value="ceiling">ceiling</option><option value="wall">wall</option></select></label>
    </div>
    <div class="jobs" id="jobs"><p class="empty">No jobs yet.</p></div>
    <h2 style="margin-top:var(--space-lg)">Log</h2>
    <div class="log" id="log" aria-live="polite">—</div>
  </section>
  <section>
    <h2>Outputs</h2>
    <div class="player" id="player" hidden>
      <video id="video" controls playsinline preload="metadata"></video>
      <div class="cap" id="cap"></div>
    </div>
    <div class="outs" id="outs"><p class="empty">Nothing converted yet.</p></div>
  </section>
</main>
<script>
(() => {
  const $ = (s) => document.querySelector(s);
  const H = { "X-G64conv": "1", "Content-Type": "application/json" };
  const fmtBytes = (n) => n < 1e6 ? (n / 1e3).toFixed(0) + " kB" : n < 1e9 ? (n / 1e6).toFixed(1) + " MB" : (n / 1e9).toFixed(2) + " GB";
  const fmtDur = (s) => { if (s == null) return "?"; const m = Math.floor(s / 60), h = Math.floor(m / 60);
    return (h ? h + "h " : "") + (m % 60) + "m " + Math.round(s % 60) + "s"; };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  let state = null, playing = null, localLog = [];

  function opts() {
    return { formats: [...document.querySelectorAll('input[name=fmt]:checked')].map((i) => i.value),
             dewarp: $("#dewarp").value, mount: $("#mount").value };
  }
  function note(s) { localLog.push(new Date().toTimeString().slice(0, 8) + "  " + s); renderLog(); }

  async function api(path, body) {
    const r = await fetch(path, { method: "POST", headers: H, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }

  function upload(file) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("PUT", "/api/upload?name=" + encodeURIComponent(file.name));
      x.setRequestHeader("X-G64conv", "1");
      x.upload.onprogress = (e) => { if (e.lengthComputable) note(`uploading ${file.name}: ${Math.round(100 * e.loaded / e.total)}%`); };
      x.onload = () => { try { const j = JSON.parse(x.responseText); x.status === 200 ? resolve(j) : reject(new Error(j.error)); } catch (e) { reject(e); } };
      x.onerror = () => reject(new Error("upload failed"));
      x.send(file);
    });
  }

  async function convertFiles(files) {
    const accepted = [...files].filter((f) => /\.g64x?$/i.test(f.name));
    if (!accepted.length) { note("no .g64 / .g64x files in the drop"); return; }
    try {
      const paths = [];
      for (const f of accepted) { note(`uploading ${f.name} (${fmtBytes(f.size)})`); const j = await upload(f); paths.push(j.path); }
      note(`upload done, converting ${paths.length} archive(s)`);
      await api("/api/convert", { paths, ...opts() });
      refresh(true);
    } catch (e) { note("ERROR " + e.message); }
  }

  async function convertPath() {
    const p = $("#path").value.trim();
    if (!p) return;
    try { await api("/api/convert", { paths: [p], ...opts() }); note("queued " + p); $("#path").value = ""; refresh(true); }
    catch (e) { note("ERROR " + e.message); }
  }

  // drop zone
  const drop = $("#drop");
  drop.addEventListener("click", () => $("#file").click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file").click(); } });
  $("#file").addEventListener("change", (e) => { convertFiles(e.target.files); e.target.value = ""; });
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => convertFiles(e.dataTransfer.files));
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => e.preventDefault());
  $("#convert-path").addEventListener("click", convertPath);
  $("#path").addEventListener("keydown", (e) => { if (e.key === "Enter") convertPath(); });

  // outputs
  function play(o) {
    playing = o.name;
    $("#player").hidden = false;
    const v = $("#video");
    v.src = o.url; v.play().catch(() => {});
    $("#cap").textContent = `${o.name} · ${o.width}x${o.height} ${o.codec || ""} · ${fmtDur(o.duration)}` +
      (o.rotation ? ` · display rotation ${o.rotation}` : "");
    renderOutputs();
  }
  async function act(kind, o, extra) {
    try { await api(kind === "dewarp" ? "/api/dewarp" : "/api/transcode", { paths: [state.out_dir + "/" + o.name], ...extra });
      note(`queued ${kind} of ${o.name}`); refresh(true); }
    catch (e) { note("ERROR " + e.message); }
  }
  function renderOutputs() {
    const el = $("#outs");
    if (!state || !state.outputs.length) { el.innerHTML = '<p class="empty">Nothing converted yet.</p>'; return; }
    el.innerHTML = "";
    for (const o of state.outputs) {
      const d = document.createElement("div");
      d.className = "out" + (o.name === playing ? " playing" : "");
      const facts = o.kind === "dir"
        ? `${o.entries} files`
        : [o.width ? `${o.width}x${o.height}` : null, o.codec, o.frames != null ? `${o.frames} frames` : null,
           o.duration ? fmtDur(o.duration) : null, o.rotation ? `rotation ${o.rotation}` : null, fmtBytes(o.size)].filter(Boolean).join(" · ");
      const verified = verifyFor(o.name);
      d.innerHTML = `<div><div class="name">${esc(o.name)}</div><div class="facts">${esc(facts)}${verified ? "<br>" + esc(verified) : ""}</div></div><div class="acts"></div>`;
      const acts = d.querySelector(".acts");
      const mk = (label, fn, primary) => { const b = document.createElement("button"); b.textContent = label; if (primary) b.className = "primary"; b.onclick = fn; acts.appendChild(b); };
      if (o.playable) mk("Play", () => play(o), true);
      const a = document.createElement("a"); a.href = o.url; a.textContent = o.kind === "dir" ? "List" : "Download";
      if (o.kind !== "dir") a.setAttribute("download", o.name.split("/").pop());
      a.style.cssText = "align-self:center;padding:0 8px"; acts.appendChild(a);
      if (/\.mp4$/i.test(o.name)) {
        const sel = document.createElement("select"); sel.setAttribute("aria-label", "produce format");
        sel.innerHTML = '<option value="">format…</option>' + ["mkv", "mov", "webm", "gif", "frames", "hls"].map((f) => `<option>${f}</option>`).join("");
        sel.onchange = () => { if (sel.value) { act("transcode", o, { formats: [sel.value] }); sel.value = ""; } };
        acts.appendChild(sel);
        if (o.width && o.width === o.height) mk("Dewarp", () => act("dewarp", o, { mode: $("#dewarp").value === "panorama" ? "panorama" : "double", mount: $("#mount").value }));
      }
      el.appendChild(d);
    }
  }
  function verifyFor(name) {
    if (!state) return "";
    for (const j of state.jobs) for (const r of j.results || []) {
      if (r.output && r.output.endsWith("/" + name) && r.verify) {
        const v = r.verify;
        return `${r.status}: ${r.frames_written} frames written, ${v.mp4_decoded_frames} decoded, ${r.frames_damaged} damaged in source, ${v.decode_errors} decode errors`;
      }
    }
    return "";
  }
  function renderJobs() {
    const el = $("#jobs");
    if (!state || !state.jobs.length) { el.innerHTML = '<p class="empty">No jobs yet.</p>'; return; }
    el.innerHTML = "";
    for (const j of state.jobs) {
      const d = document.createElement("div");
      const unknown = j.progress < 0 && j.status === "running";
      d.className = "job " + j.status + (unknown ? " unknown" : "");
      const pct = Math.round(Math.max(j.progress, 0) * 100);
      d.innerHTML = `<header><span><b>${esc(j.status.toUpperCase())}</b> ${esc(j.kind)} · ${j.inputs.map((p) => esc(p.split("/").pop())).join(", ")}</span><span>${j.status === "running" ? (unknown ? "…" : pct + "%") : ""}</span></header>` +
        `<div class="bar"><i style="width:${j.status === "running" ? pct : 100}%"></i></div>` +
        (j.message ? `<div class="facts" style="font-size:var(--text-sm);color:var(--color-ink-2)">${esc(j.message)}</div>` : "");
      el.appendChild(d);
    }
  }
  function renderLog() {
    const lines = [...localLog];
    if (state) for (const j of [...state.jobs].reverse()) for (const l of j.log) lines.push(`${j.id}  ${l}`);
    lines.sort();
    const el = $("#log"); el.textContent = lines.length ? lines.join("\n") : "—"; el.scrollTop = el.scrollHeight;
  }
  function renderStatus() {
    const t = state.tools;
    const el = $("#st-tools");
    el.textContent = t.ffmpeg && t.ffprobe ? "ffmpeg ok" : "ffmpeg NOT FOUND — install it, then reload";
    el.className = t.ffmpeg && t.ffprobe ? "v" : "bad";
    $("#st-out").textContent = "outputs → " + state.out_dir;
    $("#st-busy").textContent = state.busy ? "working" : "idle";
  }

  let timer = null;
  async function refresh(soon) {
    clearTimeout(timer);
    try {
      const r = await fetch("/api/state", { headers: { "X-G64conv": "1" } });
      state = await r.json();
      renderStatus(); renderJobs(); renderOutputs(); renderLog();
    } catch (e) { $("#st-tools").textContent = "server unreachable"; $("#st-tools").className = "bad"; }
    timer = setTimeout(refresh, soon || (state && state.busy) ? 1000 : 4000);
  }
  refresh(true);
})();
</script>
</body>
</html>
"""
