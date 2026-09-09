"""Source download retries must never weaken the pinned checksum boundary."""

import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "g64conv_build_sources", Path(__file__).parents[1] / "packaging" / "build_sources.py"
)
assert SPEC is not None and SPEC.loader is not None
sources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sources)


def source_archive():
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:bz2") as archive:
        member = tarfile.TarInfo("source/COPYING")
        member.size = 7
        archive.addfile(member, io.BytesIO(b"license"))
    return result.getvalue()


def package_for(data):
    return {"name": "fixture", "source_url": "https://example.invalid/source.tar.bz2",
            "retry_url": "https://example.invalid/source.tar.bz2?inline=false",
            "sha256": hashlib.sha256(data).hexdigest()}


def responses(monkeypatch, bodies):
    requests = []
    iterator = iter(bodies)

    def open_response(request, timeout):
        requests.append(request)
        response = io.BytesIO(next(iterator))
        response.headers = {"Content-Type": "application/octet-stream"}
        return response

    monkeypatch.setattr(sources.urllib.request, "urlopen", open_response)
    monkeypatch.setattr(sources.time, "sleep", lambda seconds: None)
    return requests


def test_bad_bad_good_response_keeps_original_pin(tmp_path, monkeypatch, capsys):
    original = source_archive()
    package = package_for(original)
    requests = responses(monkeypatch, [b"<html>try later</html>", b"truncated archive", original])
    cached = sources.collect(package, tmp_path)
    assert cached.read_bytes() == original
    assert package["sha256"] == hashlib.sha256(original).hexdigest()
    assert len(requests) == 3
    assert requests[0].full_url == package["source_url"]
    assert requests[1].full_url == requests[2].full_url == package["retry_url"]
    assert requests[1].get_header("Cache-control") == "no-cache"
    assert "try later" in capsys.readouterr().err


def test_all_bad_responses_fail_without_caching(tmp_path, monkeypatch):
    package = package_for(source_archive())
    requests = responses(monkeypatch, [b"bad response"] * 3)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        sources.collect(package, tmp_path)
    assert len(requests) == 3
    assert not (tmp_path / "fixture.source").exists()


def test_corrupt_cache_is_revalidated(tmp_path, monkeypatch):
    original = source_archive()
    (tmp_path / "fixture.source").write_bytes(b"unverified old cache")
    requests = responses(monkeypatch, [original])
    assert sources.collect(package_for(original), tmp_path).read_bytes() == original
    assert len(requests) == 1


def test_matching_digest_is_not_enough_for_non_archive(tmp_path, monkeypatch):
    body = b"<html>this is not source code</html>"
    responses(monkeypatch, [body] * 3)
    with pytest.raises(RuntimeError, match="Invalid source archive"):
        sources.collect(package_for(body), tmp_path)
    assert not (tmp_path / "fixture.source").exists()
