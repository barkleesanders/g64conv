"""Collect checksum-pinned original codec sources and their build recipe.

Produces a companion release archive, never installed with the application.
Source archives stay unchanged and compressed inside it; no third-party build
script is executed. Requires the GitHub CLI for authenticated GitHub API work.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate_archive(data: bytes, package: dict[str, str]) -> None:
    digest = hashlib.sha256(data).hexdigest()
    if digest != package["sha256"]:
        # A CDN or rate-limit page can be returned with HTTP 200. Keep enough
        # diagnostic context to distinguish it from a changed source archive.
        preview = repr(data[:160]) if data.lstrip().startswith((b"<", b"{")) else repr(data[:8])
        raise RuntimeError(f"Source checksum mismatch for {package['name']}: {digest}; "
                           f"bytes={len(data)}; prefix={preview}")
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            if not archive.getmembers():
                raise RuntimeError(f"Empty source archive for {package['name']}")
    except tarfile.TarError as exc:
        raise RuntimeError(f"Invalid source archive for {package['name']}") from exc


def collect(package: dict[str, str], cache: Path) -> Path:
    cached = cache / (package["name"] + ".source")
    if cached.is_file() and hashlib.sha256(cached.read_bytes()).hexdigest() == package["sha256"]:
        validate_archive(cached.read_bytes(), package)
        return cached
    # Do not relax a pin after a mismatch. A subsequent response must match
    # the original digest, and no unverified response reaches the source cache.
    for attempt in range(3):
        content_type = "unknown"
        try:
            if "github_api" in package:
                gh = shutil.which("gh")
                if not gh:
                    raise RuntimeError("Install the GitHub CLI (gh) to retrieve the pinned build recipe")
                data = subprocess.run([gh, "api", package["github_api"]], capture_output=True, check=True, timeout=90).stdout
            else:
                # Some upstream manifests use HTTP; the same checksum-verified
                # files are available over HTTPS. Never downgrade transport.
                url = package.get("retry_url", package["source_url"]) if attempt else package["source_url"]
                headers = {"User-Agent": "g64conv-source-packager/0.2"}
                if attempt:
                    headers["Cache-Control"] = "no-cache"
                request = urllib.request.Request(url.replace("http://", "https://"), headers=headers)
                with urllib.request.urlopen(request, timeout=90) as response:
                    content_type = response.headers.get("Content-Type", "unknown")
                    data = response.read()
            validate_archive(data, package)
        except (RuntimeError, urllib.error.URLError, http.client.HTTPException, TimeoutError,
                subprocess.SubprocessError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Could not obtain verified {package['name']} sources after 3 attempts "
                                   f"(content-type={content_type}): {exc}") from exc
            print(f"Retrying {package['name']} source download ({attempt + 1}/3; "
                  f"content-type={content_type}): {exc}", file=sys.stderr, flush=True)
            time.sleep(2 ** attempt)
        else:
            cached.write_bytes(data)
            return cached
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    manifest = ROOT / "packaging" / "sources.json"
    packages = json.loads(manifest.read_text())["packages"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "g64conv-codec-sources.tar.gz"
    with tempfile.TemporaryDirectory(prefix="g64conv-sources-") as temporary:
        cache = args.cache_dir or Path(temporary)
        cache.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            archives = list(pool.map(lambda package: collect(package, cache), packages))
        with tarfile.open(output, "w:gz") as bundle:
            bundle.add(manifest, arcname="g64conv-codec-sources/SOURCES.json")
            bundle.add(ROOT / "packaging" / "THIRD-PARTY.txt", arcname="g64conv-codec-sources/README.txt")
            for package, archive in zip(packages, archives, strict=True):
                # Preserve the upstream archive name for ordinary tar -xf use.
                filename = package.get("filename") or package["source_url"].rsplit("/", 1)[1]
                bundle.add(archive, arcname=f"g64conv-codec-sources/{package['name']}/{filename}")
        # Read back every bundled source and verify it against the manifest.
        with tarfile.open(output) as bundle:
            for package in packages:
                filename = package.get("filename") or package["source_url"].rsplit("/", 1)[1]
                member = bundle.extractfile(f"g64conv-codec-sources/{package['name']}/{filename}")
                if member is None or hashlib.sha256(member.read()).hexdigest() != package["sha256"]:
                    raise RuntimeError(f"Source archive readback failed for {package['name']}")
    print(f"{output}: {len(packages)} checksum-verified source archives")


if __name__ == "__main__":
    main()
