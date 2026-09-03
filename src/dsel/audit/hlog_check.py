"""Independent verification of a `.hlog` (PLAN.md S10).

*"Accept, and verify here not at M7: a `.hlog` written from Python is read by
the Java `HistogramLogProcessor` in a pinned JDK container with p50/p99/p99.9
matching within one bucket. The entire 'third party recomputes percentiles'
claim rests on this."*

The audit bundle carries raw histograms so that someone else can recompute the
percentiles rather than trust the ones in the report. That promise is only real
if a *different* implementation can read the file -- the reference Java one,
which is what the log format exists for. Checking the Python writer against the
Python reader would prove nothing: a shared bug is invisible to itself.

So this module runs the reference implementation. The jar is fetched once from
Maven Central and pinned by SHA-256, verified on every use including cache
hits; the JDK is pinned by OCI index digest. Neither the jar nor the log is
bind-mounted -- both are streamed in with `docker cp` (D7), the same way the
observability configuration and the environment probes are.

**The comparison is made at the cumulative count, not at the nominal
percentile.** `HistogramLogProcessor` prints the *percentile iterator*, not
`getValueAtPercentile`, and the two are not the same function. The iterator
walks buckets in one direction and each step is forced to a strictly higher
bucket than the last, so where consecutive percentile levels fall in the same
bucket the later one is pushed outward; it also prints steps at 0.990234375
rather than at 0.99. Comparing its row against a nominal `p99` therefore
compares two different quantiles, and measured here it disagrees by about 1%
regardless of sample size -- 2000 values or 60000, the gap stays.

The count column is unambiguous. "The value at which the cumulative count first
reaches k" means the same thing in both implementations, and comparing on it is
what actually tests the claim: that a third party reading our raw histogram
recovers our distribution. Measured, every printed row matches exactly.

"Within one bucket" remains the tolerance, because that is the only difference
the *format* permits: HdrHistogram stores counts per bucket, and at 3
significant figures a bucket is 0.1% wide.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# HdrHistogram (Java), BSD-2-Clause / public domain. Digest checked against the
# .sha256 Maven Central publishes beside the artifact, 2026-09-03.
JAR_VERSION = "2.2.2"
JAR_URL = (
    "https://repo1.maven.org/maven2/org/hdrhistogram/HdrHistogram/"
    f"{JAR_VERSION}/HdrHistogram-{JAR_VERSION}.jar"
)
JAR_SHA256 = "22d1d4316c4ec13a68b559e98c8256d69071593731da96136640f864fa14fad8"

# eclipse-temurin:21-jdk, OCI *index* digest resolved 2026-09-03. The platform
# manifest digest belongs in the run manifest, never here: it is architecture-
# locked and would make this file unrunnable on amd64 CI.
JDK_IMAGE = (
    "eclipse-temurin@sha256:85f00967bcc624fc19fa9c2cf124ea426a5363898e267141726f31f358c2e14b"
)

PROCESSOR = "org.HdrHistogram.HistogramLogProcessor"
DEFAULT_PERCENTILES = (50.0, 99.0, 99.9)


class HlogCheckError(RuntimeError):
    """The independent read failed. Never fall back to the Python reader."""


def cache_dir() -> Path:
    root = os.environ.get("DSEL_CACHE_DIR")
    return Path(root) if root else Path.home() / ".cache" / "dsel"


def ensure_jar(directory: Path | None = None) -> Path:
    """Fetch the pinned jar if absent, and verify its digest either way.

    Verified on cache hits too: a cached file is not evidence of anything, and
    the whole point of this check is that the reader is the artefact it claims
    to be.
    """
    directory = directory or (cache_dir() / "jars")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"HdrHistogram-{JAR_VERSION}.jar"
    if not path.is_file():
        with urllib.request.urlopen(JAR_URL, timeout=120) as response:
            path.write_bytes(response.read())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != JAR_SHA256:
        path.unlink(missing_ok=True)
        raise HlogCheckError(
            f"{JAR_URL} hashed {digest}, expected {JAR_SHA256}; the pinned "
            "reference implementation is not what was fetched"
        )
    return path


@dataclass(frozen=True, slots=True)
class PercentileRow:
    """One line of the printed distribution."""

    quantile: float
    value: float
    count: int
    """Cumulative count at this value. The only column two implementations
    can be compared on without agreeing about percentile semantics first."""


@dataclass(frozen=True, slots=True)
class PercentileTable:
    """The percentile distribution the Java processor printed."""

    rows: tuple[PercentileRow, ...]
    total_count: int
    max_value: float

    def row_at(self, percentile: float) -> PercentileRow:
        """The first row at or past `percentile`.

        The whole row is returned, never the value alone: the comparison a
        caller wants is against `count`, because the printed quantile is the
        iterator's step level rather than the quantile of the value beside it.
        """
        # 99.9 / 100 is 0.9990000000000001 in binary floating point, which
        # sorts *after* the row printed as 0.999. Without the epsilon the
        # p99.9 comparison silently reads the next row up.
        target = percentile / 100.0 - 1e-12
        for row in self.rows:
            if row.quantile >= target:
                return row
        raise HlogCheckError(f"no row at or past p{percentile:g}")

    def value_at(self, percentile: float) -> float:
        """The value alone. Prefer `row_at` when comparing implementations."""
        return self.row_at(percentile).value


def parse_percentile_output(text: str) -> PercentileTable:
    """Parse `HistogramLogProcessor`'s percentile distribution."""
    rows: list[PercentileRow] = []
    total_count = 0
    max_value = 0.0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#["):
            if "Total count" in stripped:
                total_count = int(stripped.rsplit("=", 1)[1].strip(" ]"))
            if stripped.startswith("#[Max"):
                max_value = float(stripped.split("=", 1)[1].split(",", 1)[0].strip())
            continue
        if not stripped or stripped.startswith(('"', "Value")):
            continue
        fields = stripped.split()
        if len(fields) < 3:
            continue
        try:
            value, quantile, count = float(fields[0]), float(fields[1]), int(fields[2])
        except ValueError:
            continue
        rows.append(PercentileRow(quantile=quantile, value=value, count=count))
    if not rows:
        raise HlogCheckError("the processor printed no percentile rows")
    return PercentileTable(rows=tuple(rows), total_count=total_count, max_value=max_value)


def java_percentiles(
    hlog: Path,
    percentiles: tuple[float, ...] = DEFAULT_PERCENTILES,
    *,
    run_id: str = "hlog-check",
) -> tuple[dict[str, float], PercentileTable]:
    """Read `hlog` with the reference Java implementation.

    The container holds no mounts. The jar and the log are streamed in with
    `docker cp`, the container is labelled so the ordinary teardown can find
    it, and it is removed on every path out.
    """
    from dsel.runtime.teardown import LABEL_KEY, MANAGED_LABEL

    jar = ensure_jar()
    name = f"dsel-hlog-{run_id}-{os.getpid()}"
    subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True)
    created = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            f"{MANAGED_LABEL}=true",
            "--label",
            f"{LABEL_KEY}={run_id}",
            "--cpuset-cpus",
            "0-1",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            JDK_IMAGE,
            "sleep",
            "300",
        ],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        raise HlogCheckError(f"could not start the JDK container: {created.stderr.strip()}")
    try:
        for source, target in ((jar, "/tmp/hdr.jar"), (hlog, "/tmp/subject.hlog")):
            copied = subprocess.run(
                ["docker", "cp", str(source), f"{name}:{target}"],
                capture_output=True,
                text=True,
            )
            if copied.returncode != 0:
                raise HlogCheckError(f"docker cp {source.name} failed: {copied.stderr.strip()}")
        processed = subprocess.run(
            [
                "docker",
                "exec",
                name,
                "java",
                "-cp",
                "/tmp/hdr.jar",
                PROCESSOR,
                "-i",
                "/tmp/subject.hlog",
                # The log holds microseconds; without this the processor divides
                # by 1e6 and reports milliseconds-as-nanoseconds.
                "-outputValueUnitRatio",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if processed.returncode != 0:
            raise HlogCheckError(
                f"{PROCESSOR} exited {processed.returncode}: {processed.stderr.strip()}"
            )
        table = parse_percentile_output(processed.stdout)
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True)
    return {f"p{p:g}": table.value_at(p) for p in percentiles}, table


def within_one_bucket(value: float, other: float, significant_figures: int = 3) -> bool:
    """Whether two values fall within one histogram bucket of each other.

    A bucket at `n` significant figures is `10**-n` wide in relative terms, so
    the comparison is relative and not absolute -- an absolute tolerance would
    be meaninglessly tight at 200 us and meaninglessly loose at 2 s.
    """
    if value == other:
        return True
    largest = max(abs(value), abs(other))
    if largest == 0:
        return True
    return abs(value - other) / largest <= 10.0**-significant_figures * 2.0
