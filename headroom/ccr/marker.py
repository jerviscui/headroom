"""Central definition of the terse CCR retrieval marker (``[ccr:<id>]``).

The verbose marker — ``[N items compressed to M. Retrieve more: hash=<id>]`` —
spends ~15 tokens of fixed boilerplate on every compressed block just to say
"you can retrieve this." When ``HEADROOM_CCR_TERSE_MARKER`` is set, producers
emit the terse ``[ccr:<id>]`` form instead (~4 tokens) and the retrieval
instructions move, once, into the injected tool description (see
``ccr/tool_injection.py``). Off by default while retrieval reliability with the
terse form is validated against a live model.

``<id>`` is whatever the store returns — a short adaptive label when
``HEADROOM_CCR_SHORT_LABELS`` is on, else the full 24-hex hash. The two knobs are
orthogonal and compound: short-label shrinks the id, terse-marker shrinks the
boilerplate around it.
"""

from __future__ import annotations

import os
import re

# Terse retrieval marker: ``[ccr:<id>]``. The id is any run of non-bracket,
# non-space characters, so the form stays encoding-agnostic (hex today,
# base32/wordlist later) and the capture group yields the id to retrieve.
CCR_TERSE_MARKER_RE = re.compile(r"\[ccr:([^\]\s]+)\]")


def terse_markers_enabled() -> bool:
    """True when producers should emit ``[ccr:<id>]`` instead of the verbose marker."""
    return os.environ.get("HEADROOM_CCR_TERSE_MARKER", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def terse_marker(label: str) -> str:
    """The terse retrieval marker naming a stored content ``label``."""
    return f"[ccr:{label}]"
