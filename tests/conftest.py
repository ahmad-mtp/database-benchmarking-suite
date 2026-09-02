"""Shared fixtures: Docker availability gating."""

from __future__ import annotations

import subprocess

import pytest


def _daemon_available() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=20, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


DOCKER_AVAILABLE = _daemon_available()

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="Docker daemon is not running"
)
