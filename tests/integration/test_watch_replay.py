"""S8a acceptance: a live session and a replay must land in the same state.

*PLAN.md S8a:* "`dsel watch --replay <run-id>` on a finished run reaches the
same final screen state as the live session did, and the warmup -> measure
boundary is visibly marked in both."

This runs it as three real processes, not as one in-process simulation:

1. a writer appending shards over several seconds;
2. `dsel watch --run` following those files while they grow;
3. `dsel watch --replay` over the merged file once the run has finished.

Both watchers dump the final state and the final screen through the same
functions that drive the terminal, and the two dumps are compared byte for
byte. Doing it in-process would compare the reducer against itself and prove
nothing about the tailer, which is the part that can actually diverge.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dsel.live.merge import merge_to_file
from dsel.runtime.paths import RunLayout, new_run_id

WRITER_SECONDS = 6.0
IDLE_TIMEOUT_SECONDS = 2.5
BOUNDARY = "┃"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _dsel() -> list[str]:
    """The console script if it is on PATH, else the module. Same entry point."""
    found = shutil.which("dsel")
    return [found] if found else [sys.executable, "-m", "dsel.cli"]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    return env


@pytest.mark.slow
def test_live_and_replay_reach_the_same_screen(tmp_path: Path) -> None:
    layout = RunLayout.for_run(new_run_id(), base=tmp_path / "runs")
    layout.create()
    run_dir = layout.root

    writer = subprocess.Popen(
        [sys.executable, "-m", "tests.support.fake_run", str(run_dir), str(WRITER_SECONDS)],
        cwd=REPO_ROOT,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    live = subprocess.run(
        [
            *_dsel(),
            "watch",
            "--run",
            str(run_dir),
            "--idle-timeout",
            str(IDLE_TIMEOUT_SECONDS),
            "--state-out",
            str(tmp_path / "live.json"),
            "--screen-out",
            str(tmp_path / "live.txt"),
        ],
        cwd=REPO_ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=WRITER_SECONDS + 60,
    )
    writer_out, writer_err = writer.communicate(timeout=30)
    assert writer.returncode == 0, writer_err
    assert live.returncode == 0, live.stderr

    written = int(writer_out.split()[1])
    assert written > 100, f"the run should be substantial, got {writer_out!r}"

    # A finished run has a merged metrics.ndjson; that is what replay reads.
    merged = merge_to_file(layout.shards, layout.metrics)
    assert merged == written, "the merge must carry every record the run wrote"

    replay = subprocess.run(
        [
            *_dsel(),
            "watch",
            "--replay",
            str(run_dir),
            "--state-out",
            str(tmp_path / "replay.json"),
            "--screen-out",
            str(tmp_path / "replay.txt"),
        ],
        cwd=REPO_ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert replay.returncode == 0, replay.stderr

    live_state = (tmp_path / "live.json").read_text(encoding="utf-8")
    replay_state = (tmp_path / "replay.json").read_text(encoding="utf-8")
    assert live_state == replay_state, "the live session and the replay disagree"

    live_screen = (tmp_path / "live.txt").read_text(encoding="utf-8")
    replay_screen = (tmp_path / "replay.txt").read_text(encoding="utf-8")
    assert live_screen == replay_screen

    # The state must be the whole run, not a prefix the tailer never released.
    assert f'"records_seen": {written}' in live_state
    assert '"warmup_ended_t_ms": null' not in live_state
    assert '"measure_began_t_ms": null' not in live_state

    for name, screen in (("live", live_screen), ("replay", replay_screen)):
        assert BOUNDARY in screen, f"{name} screen does not mark the boundary"
        bar = next(line for line in screen.splitlines() if " warmup " in line)
        before, after = bar.split(BOUNDARY, 1)
        assert "warmup" in before, f"{name}: boundary is not after warmup"
        assert "measure" in after, f"{name}: boundary is not before measure"
