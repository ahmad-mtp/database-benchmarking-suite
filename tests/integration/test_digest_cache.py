"""Image resolution is local-first and its cache cannot go stale.

An index digest is a content address, so the index -> platform mapping stored
under it is immutable. Caching it keeps a run from spending a Docker Hub
rate-limit token to re-learn a fact that cannot change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsel.audit.environment import (
    CaptureError,
    _cache_path,
    _local_index_digest,
    resolve_image,
)
from tests.conftest import requires_docker

IMAGE = "python:3.13-slim"


@requires_docker
def test_index_digest_reads_from_the_local_daemon() -> None:
    digest = _local_index_digest(IMAGE)
    assert digest is not None and digest.startswith("sha256:")


def test_absent_image_returns_none_not_an_error() -> None:
    assert _local_index_digest("dsel-nonexistent-image:does-not-exist") is None


@requires_docker
def test_resolution_is_served_from_the_cache_without_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed the cache with a value the registry would never return, then prove
    it comes back. If the registry were consulted, this could not pass -- and
    the test needs no network at all, which is the point of the cache."""
    monkeypatch.setenv("DSEL_CACHE_DIR", str(tmp_path))
    assert _cache_path().parent == tmp_path

    index_digest = _local_index_digest(IMAGE)
    assert index_digest is not None
    sentinel = "sha256:" + "0" * 64
    _cache_path().parent.mkdir(parents=True, exist_ok=True)
    _cache_path().write_text(
        json.dumps({index_digest: {"platform_digest": sentinel, "platform": "linux/test"}})
    )

    pin = resolve_image(IMAGE)
    assert pin.index_digest == index_digest
    assert pin.platform_digest == sentinel
    assert pin.platform == "linux/test"


@requires_docker
def test_cache_is_written_on_a_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolution that had to consult the registry must be remembered.

    Skipped when the registry is unreachable or rate-limited: what is being
    checked is the write-back, not Docker Hub's availability.
    """
    monkeypatch.setenv("DSEL_CACHE_DIR", str(tmp_path))
    try:
        pin = resolve_image(IMAGE)
    except CaptureError as exc:
        pytest.skip(f"registry unavailable: {exc}")
    cache = json.loads(_cache_path().read_text())
    assert cache[pin.index_digest]["platform_digest"] == pin.platform_digest
    assert not pin.platform.startswith("unknown")


@requires_docker
def test_no_pull_mode_refuses_an_absent_image(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(CaptureError, match="not present locally"):
        resolve_image("dsel-nonexistent-image:does-not-exist", allow_pull=False)


def test_corrupt_cache_is_ignored_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DSEL_CACHE_DIR", str(tmp_path))
    _cache_path().parent.mkdir(parents=True, exist_ok=True)
    _cache_path().write_text("{not json")
    from dsel.audit.environment import _load_cache

    assert _load_cache() == {}
