"""Named-volume enforcement (PLAN.md S3-S5).

exp04/exp05: bind-mounted storage raised run-to-run variance from 1.3% to 18%
and blinded `docker stats` BlockIO, which reported 8.19 kB for 6.44 GB of
writes. `ResourceEnvelope.storage` therefore has exactly one legal value, and
the filesystem backing the mount is asserted at runtime rather than assumed --
a volume can be created correctly and still land on the wrong filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass

# `stat -f -c %T` on a Docker Desktop named volume. The VM's volume store is
# ext4, which `stat` names "ext2/ext3" -- they share a magic number.
EXPECTED_FSTYPES = frozenset({"ext2/ext3", "ext4", "xfs"})

# What a VirtioFS bind mount reports instead. Named explicitly so the error
# says what actually happened rather than "unexpected filesystem".
BIND_MOUNT_FSTYPES = frozenset({"fuseblk", "virtiofs", "osxfuse", "9p", "overlayfs", "UNKNOWN"})


class StorageError(RuntimeError):
    """The data directory is not on a named volume of the expected kind."""


@dataclass(frozen=True, slots=True)
class VolumeMount:
    """A named volume mounted into a container."""

    volume: str
    target: str

    def docker_flags(self) -> list[str]:
        return ["--volume", f"{self.volume}:{self.target}"]


def check_fstype(fstype: str, target: str) -> None:
    """Assert the mount's filesystem is a named volume's, not a bind mount's."""
    cleaned = fstype.strip()
    if cleaned in EXPECTED_FSTYPES:
        return
    if cleaned in BIND_MOUNT_FSTYPES:
        raise StorageError(
            f"{target} reports filesystem {cleaned!r}, which is a bind mount. "
            "exp04 measured bind mounts raising run-to-run variance from 1.3% to 18% "
            "and reporting 8.19 kB of BlockIO for 6.44 GB of writes. Named volumes only."
        )
    raise StorageError(
        f"{target} reports filesystem {cleaned!r}, which is not a known named-volume "
        f"filesystem (expected one of {sorted(EXPECTED_FSTYPES)})"
    )
