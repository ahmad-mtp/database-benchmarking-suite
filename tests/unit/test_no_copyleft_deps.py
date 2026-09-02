"""No GPL in the toolchain (PLAN.md, locked decisions).

PLAN.md rules copyleft out of the toolchain -- that is what disqualified
`memtier_benchmark`, and Grafana's AGPL is why D9 builds the TUI first. A
transitive dependency can reintroduce it silently, so the installed environment
is checked rather than only the declared dependency list.

CLAUDE.md also names SDV (BUSL-1.1) and MongoDB (SSPL) as landmines already
found, so those families are refused here too.
"""

from __future__ import annotations

import re
from importlib.metadata import distributions

# Matched against SPDX-ish licence metadata. LGPL and GPL-with-exception cases
# would be a deliberate decision, so they trip the guard and get looked at.
FORBIDDEN = re.compile(
    r"\b(?:A?GPL|LGPL|GNU\s+(?:Affero\s+|Lesser\s+)?General\s+Public|SSPL|BUSL|"
    r"Business\s+Source|Server\s+Side\s+Public)\b",
    re.IGNORECASE,
)

# Licence metadata is unreliable enough that a classifier is preferred where
# present; these fields are all consulted.
_FIELDS = ("License-Expression", "License", "Classifier")


def _licence_text(dist) -> str:  # noqa: ANN001 - importlib Distribution
    meta = dist.metadata
    parts: list[str] = []
    for field in _FIELDS:
        values = meta.get_all(field) or []
        parts.extend(v for v in values if isinstance(v, str))
    return " | ".join(parts)


def test_no_copyleft_distribution_is_installed() -> None:
    offenders: list[str] = []
    for dist in distributions():
        name = dist.metadata.get("Name") or "<unnamed>"
        text = _licence_text(dist)
        if FORBIDDEN.search(text):
            offenders.append(f"{name} {dist.version}: {text}")
    assert not offenders, (
        "copyleft/source-available licences found in the toolchain, which "
        "PLAN.md rules out:\n  " + "\n  ".join(sorted(offenders))
    )
