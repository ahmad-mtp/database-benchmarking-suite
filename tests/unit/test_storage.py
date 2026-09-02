"""Named-volume enforcement (PLAN.md S3-S5, exp04/exp05)."""

from __future__ import annotations

import pytest

from dsel.runtime.storage import StorageError, VolumeMount, check_fstype


@pytest.mark.parametrize("fstype", ["ext2/ext3", "ext4", "xfs", "  ext2/ext3  "])
def test_named_volume_filesystems_are_accepted(fstype: str) -> None:
    check_fstype(fstype, "/var/lib/postgresql/data")


@pytest.mark.parametrize("fstype", ["virtiofs", "fuseblk", "9p", "osxfuse", "overlayfs"])
def test_bind_mount_filesystems_are_refused(fstype: str) -> None:
    with pytest.raises(StorageError, match="bind mount"):
        check_fstype(fstype, "/var/lib/postgresql/data")


def test_bind_mount_refusal_cites_the_measurement() -> None:
    """The error should say why, not just that it is disallowed."""
    with pytest.raises(StorageError) as exc:
        check_fstype("virtiofs", "/data")
    assert "1.3%" in str(exc.value) and "18%" in str(exc.value)


def test_unknown_filesystem_is_refused_not_assumed_fine() -> None:
    with pytest.raises(StorageError, match="not a known named-volume"):
        check_fstype("btrfs-on-mars", "/data")


def test_volume_mount_flags() -> None:
    mount = VolumeMount(volume="run-data", target="/var/lib/postgresql/data")
    assert mount.docker_flags() == ["--volume", "run-data:/var/lib/postgresql/data"]
