"""Terse CCR marker ``[ccr:<id>]`` + variable-width id detection.

With HEADROOM_CCR_TERSE_MARKER on, producers collapse the ~15-token verbose
boilerplate to ``[ccr:<id>]`` (~4 tokens) and the retrieval instruction moves
into the injected tool description. Detection (parser, compression_units,
tool_injection) also learns variable-width hex ids (2-24) — which is what makes
the adaptive short-label feature actually retrievable end-to-end.
"""

from __future__ import annotations

from headroom.cache.backends import InMemoryBackend
from headroom.cache.compression_store import CompressionStore
from headroom.ccr.marker import (
    CCR_TERSE_MARKER_RE,
    terse_marker,
    terse_markers_enabled,
)
from headroom.ccr.tool_injection import CCRToolInjector
from headroom.parser import CCR_RETRIEVAL_MARKER_RE
from headroom.transforms.compression_units import _CCR_MARKER_RE


def test_terse_marker_format():
    assert terse_marker("f2") == "[ccr:f2]"


def test_terse_markers_enabled_env(monkeypatch):
    monkeypatch.delenv("HEADROOM_CCR_TERSE_MARKER", raising=False)
    assert terse_markers_enabled() is False
    monkeypatch.setenv("HEADROOM_CCR_TERSE_MARKER", "on")
    assert terse_markers_enabled() is True


def test_terse_regex_extracts_id():
    m = CCR_TERSE_MARKER_RE.search("tool output [ccr:a3f] trailing")
    assert m is not None
    assert m.group(1) == "a3f"


def test_both_detection_regexes_match_terse():
    text = "some output\n[ccr:a3f]"
    assert CCR_RETRIEVAL_MARKER_RE.search(text)  # parser (reversibility guard, etc.)
    assert _CCR_MARKER_RE.search(text)  # compression_units


def test_tool_injection_detects_terse_marker():
    inj = CCRToolInjector()
    ids = inj.scan_for_markers([{"role": "tool", "content": "result body\n[ccr:a3f]"}])
    assert "a3f" in ids
    assert inj.has_compressed_content


def test_tool_injection_detects_short_hash_marker():
    # Short-label markers (hash=f2) must be detected — not only the legacy 24-hex,
    # or the retrieve tool never gets injected and retrieval breaks end-to-end.
    inj = CCRToolInjector()
    ids = inj.scan_for_markers(
        [{"role": "tool", "content": "[40 items compressed to 8. Retrieve more: hash=f2]"}]
    )
    assert "f2" in ids


def test_tool_injection_still_detects_full_hash_marker():
    inj = CCRToolInjector()
    full = "a" * 24
    ids = inj.scan_for_markers(
        [{"role": "tool", "content": f"[40 items compressed to 8. Retrieve more: hash={full}]"}]
    )
    assert full in ids


def test_terse_id_resolves_via_store_end_to_end():
    # store short label -> producer emits terse marker -> tool_injection extracts
    # the id -> store.retrieve resolves it. The whole point, wired.
    store = CompressionStore(backend=InMemoryBackend(), short_labels=True)
    content = "big tool output " * 40
    label = store.store(content, "compressed")
    marker = terse_marker(label)
    inj = CCRToolInjector()
    ids = inj.scan_for_markers([{"role": "tool", "content": f"body {marker}"}])
    assert label in ids  # the id the model would pass to headroom_retrieve
    entry = store.retrieve(label)
    assert entry is not None and entry.original_content == content
