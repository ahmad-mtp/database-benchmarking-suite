"""The README honesty check (PLAN.md S0, and its Verification section).

PLAN.md: "the first paragraph states that this harness produces mechanisms and
scaling curves, not reportable capacity numbers, and that `dsel verify` enforces
it. If that sentence ever becomes untrue, the build has drifted."

The block is delimited by HTML comments so this test anchors on a marker rather
than on prose, but each load-bearing token is asserted individually -- deleting
the paragraph and quietly weakening it must both fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[2] / "README.md"

BEGIN = "<!-- honesty:begin -->"
END = "<!-- honesty:end -->"

# Every token the paragraph must carry to still mean what it says.
REQUIRED_TOKENS = (
    "profile=local",
    "envelope_deviation=true",
    "reportable=false",
    "dsel verify",
    "mechanisms",
    "scaling curves",
)

# The refusal itself: "does not produce reportable capacity numbers", allowing
# markdown emphasis and line wrapping between the words.
REFUSAL = re.compile(
    r"does\s+\W*not\W*\s+produce\s+reportable\s+capacity\s+numbers",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def readme() -> str:
    assert README.is_file(), f"README.md missing at {README}"
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def honesty_block(readme: str) -> str:
    assert BEGIN in readme, f"README.md has no {BEGIN} marker"
    assert END in readme, f"README.md has no {END} marker"
    start = readme.index(BEGIN) + len(BEGIN)
    end = readme.index(END)
    assert start < end, "honesty markers are out of order"
    block = readme[start:end].strip()
    assert block, "the honesty block is empty"
    return block


def test_honesty_block_is_in_the_first_section(readme: str) -> None:
    """It must lead the README, not be buried below the usage instructions."""
    assert readme.index(BEGIN) < 600, (
        "the honesty block must open the README, not sit below other sections"
    )


@pytest.mark.parametrize("token", REQUIRED_TOKENS)
def test_honesty_block_carries_token(honesty_block: str, token: str) -> None:
    assert token in honesty_block, (
        f"the README honesty block no longer mentions {token!r}; "
        "weakening this paragraph means the build has drifted"
    )


def test_honesty_block_refuses_capacity_claims(honesty_block: str) -> None:
    assert REFUSAL.search(honesty_block), (
        "the README honesty block no longer says the harness does not produce "
        "reportable capacity numbers"
    )
