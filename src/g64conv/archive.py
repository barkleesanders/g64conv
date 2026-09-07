"""Input discovery: a bare .g64 segment, or a .g64x export archive (a stored zip
with a FileInfo.xml manifest listing the video segments of each source)."""

from __future__ import annotations

import datetime
import os
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .format import G64Error

VIDEO_SUFFIX = ".g64"
METADATA_SUFFIX = ".g64m"  # motion / analytics track, never video


@dataclass
class Source:
    key: str
    label: str
    segments: list[tuple[str, Callable[[], bytes]]] = field(default_factory=list)


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "video"


def discover(path: str) -> list[Source]:
    """Return the video sources in an input, each with its ordered segments."""
    low = path.lower()
    if low.endswith(".g64x"):
        return _discover_g64x(path)
    if low.endswith(VIDEO_SUFFIX):
        return [Source("0", os.path.splitext(os.path.basename(path))[0],
                       [(os.path.basename(path), lambda: open(path, "rb").read())])]
    raise G64Error(f"unsupported input {path} (expected .g64 or .g64x)")


def _parse_time(s: str) -> float:
    """Sort key for manifest timestamps. Never sort ISO strings lexically:
    '...:00Z' sorts AFTER '...:00.700Z' because 'Z' > '.'."""
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")


def _discover_g64x(path: str) -> list[Source]:
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    if "FileInfo.xml" in names:
        root = ET.fromstring(zf.read("FileInfo.xml"))
        sources = {s.get("Id"): s for s in root.iter("UniqueSource")}
        groups: dict[str, Source] = {}
        for f in root.iter("File"):
            name = f.get("FileName") or ""
            if not name.lower().endswith(VIDEO_SUFFIX):
                continue
            src = sources.get(f.get("SourceId") or "")
            if src is not None and src.get("EncoderName") == "Metadata":
                continue
            label = (src.get("CollectionName") if src is not None else None) or os.path.splitext(name)[0]
            key = f.get("SourceId") or "0"
            g = groups.setdefault(key, Source(key, label))
            g.segments.append((f.get("start") or "", name))  # type: ignore[arg-type]
        out = []
        for g in groups.values():
            g.segments.sort(key=lambda t: (_parse_time(t[0]), t[1]))
            g.segments = [(n, (lambda n=n: zf.read(n))) for _, n in g.segments]  # type: ignore[misc]
            out.append(g)
        if out:
            return out
    # No manifest: group by base name, ordering the _N continuation segments numerically.
    segs = sorted(n for n in names if n.lower().endswith(VIDEO_SUFFIX))
    if not segs:
        raise G64Error("g64x archive contains no .g64 video segments")
    groups2: dict[str, list[tuple[int, str]]] = {}
    for n in segs:
        stem = os.path.splitext(os.path.basename(n))[0]
        m = re.match(r"^(.*)_(\d+)$", stem)
        base, idx = (m.group(1), int(m.group(2))) if m else (stem, 0)
        groups2.setdefault(base, []).append((idx, n))
    return [Source(base, base, [(n, (lambda n=n: zf.read(n))) for _, n in sorted(lst)])
            for base, lst in groups2.items()]
