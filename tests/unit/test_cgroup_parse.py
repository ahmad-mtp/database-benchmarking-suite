"""cgroup v2 parsing (PLAN.md S7)."""

from __future__ import annotations

import pytest

from dsel.runtime.cgroup import parse

SAMPLE = """@@cpu.stat
usage_usec 123456
user_usec 100000
system_usec 23456
nr_periods 90
nr_throttled 7
throttled_usec 6172
@@cpu.max
400000 100000
@@cpuset.cpus.effective
2-5
@@memory.max
3221225472
@@memory.current
104857600
@@memory.events
low 0
high 0
max 3
oom 1
oom_kill 1
@@pids.current
14
@@pids.max
512
@@io.stat
254:0 rbytes=1024 wbytes=2048 rios=4 wios=8
254:16 rbytes=512 wbytes=256 rios=1 wios=2
"""


def test_parses_every_field() -> None:
    s = parse(SAMPLE)
    assert s.cpu_usage_usec == 123456
    assert s.cpu_nr_throttled == 7
    assert s.cpu_throttled_usec == 6172
    assert s.cpu_max == "400000 100000"
    assert s.cpuset_effective == "2-5"
    assert s.memory_max == 3221225472
    assert s.memory_current == 104857600
    assert s.memory_oom == 1
    assert s.memory_oom_kill == 1
    assert s.pids_current == 14
    assert s.pids_max == 512


def test_io_stat_sums_across_devices() -> None:
    s = parse(SAMPLE)
    assert s.io_read_bytes == 1024 + 512
    assert s.io_write_bytes == 2048 + 256


def test_unlimited_is_none_not_a_fake_number() -> None:
    """`max` means unlimited. Recording a sentinel integer would be a lie."""
    s = parse("@@memory.max\nmax\n@@pids.max\nmax\n")
    assert s.memory_max is None
    assert s.pids_max is None


def test_throttled_fraction() -> None:
    """PLAN.md adds a cpu_throttling gate at 5%: flag, surface, not INVALID."""
    s = parse(SAMPLE)
    assert s.throttled_fraction == pytest.approx(6172 / 123456)
    assert s.throttled_fraction is not None and s.throttled_fraction < 0.05


def test_missing_sections_yield_none_not_zero() -> None:
    """Absent is not the same as zero, and must not be reported as measured."""
    s = parse("@@cpu.max\n400000 100000\n")
    assert s.cpu_usage_usec is None
    assert s.io_read_bytes is None
    assert s.memory_current is None


def test_empty_input_is_all_none() -> None:
    s = parse("")
    assert s.cpu_usage_usec is None and s.memory_max is None
    assert s.throttled_fraction is None
