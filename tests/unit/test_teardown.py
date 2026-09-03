"""Teardown labelling (PLAN.md S3-S5, findings.md 6.5)."""

from __future__ import annotations

import pytest

from dsel.runtime.teardown import LABEL_KEY, MANAGED_LABEL, Teardown, label_flags, list_managed


def test_every_resource_carries_both_labels() -> None:
    flags = label_flags("run-1")
    assert f"{MANAGED_LABEL}=true" in flags
    assert f"{LABEL_KEY}=run-1" in flags


def test_teardown_is_scoped_to_one_run() -> None:
    """This machine carries other people's containers; never sweep by name."""
    assert "run-1" in " ".join(label_flags("run-1"))
    assert "run-2" not in " ".join(label_flags("run-1"))


def test_unknown_resource_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown resource kind"):
        list_managed("run-1", "network-maybe")


def test_register_is_idempotent() -> None:
    teardown = Teardown("run-unregistered")
    assert teardown.register() is teardown
    assert teardown.register() is teardown


def test_teardown_of_nothing_succeeds() -> None:
    """A second teardown is not an error."""
    teardown = Teardown("run-that-never-existed")
    assert teardown.run() == 0
    assert teardown.run() == 0


def test_every_kind_the_harness_creates_is_swept() -> None:
    """Networks arrived with S11 and leaked: thirteen were found stranded after
    one afternoon of calibration runs, each holding a subnet out of the
    daemon's address pool. A kind the harness creates but teardown does not
    know about is a leak that nothing reports."""
    from dsel.runtime.teardown import RESOURCE_KINDS

    assert set(RESOURCE_KINDS) == {"container", "volume", "network"}
    for kind in RESOURCE_KINDS:
        # Every kind must have a listing; an unknown one raises instead.
        list_managed("run-that-does-not-exist", kind)
