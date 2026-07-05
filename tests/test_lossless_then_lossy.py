"""A7: lossless-then-lossy dispatch.

In lossy mode (``lossless=False``) with ``lossless_then_lossy`` on, a foldable
block is FIRST byte-folded losslessly and THEN handed to the aggressive lossy
compressor (Kompress) on the folded remainder. The lossy result is kept only
when it removes a further meaningful chunk (>= 10% fewer tokens than the fold);
otherwise the pure byte-exact fold is kept, so A7 is never worse than A6.

DIFF content is never lossy-chained (Kompressing hunks breaks ``git apply``).
Kompress is mocked so these run without the ModernBERT model.
"""
from headroom.transforms.content_router import (
    ContentRouter,
    ContentRouterConfig,
    CompressionStrategy,
)
from headroom.transforms.lossless_compaction import search_unheading


def _grep_block() -> str:
    # Repeated path prefixes → search_heading folds byte-exact (word count flat).
    paths = [
        "src/services/wallet/overdraft/automated_overdraft_initiation.py",
        "src/services/wallet/overdraft/capacity_limits.py",
    ]
    return "\n".join(
        f"{p}:{ln}:    result = compute_overdraft_capacity(business_id, amount)"
        for p in paths for ln in range(1, 40)
    ) + "\n"


def _diff_block() -> str:
    files = ["foo", "bar", "baz"]
    return "\n".join(
        f"diff --git a/{f}.py b/{f}.py\n"
        f"index 1111111aaaaaaa..2222222bbbbbbb 100644\n"
        f"--- a/{f}.py\n+++ b/{f}.py\n"
        f"@@ -1,3 +1,3 @@\n-    old_{f} = 1\n+    new_{f} = 2\n     unchanged_{f}"
        for f in files
    ) + "\n"


def _router(*, lossless_then_lossy, lossless=False, ccr=False, kompress=None):
    r = ContentRouter(
        ContentRouterConfig(
            lossless=lossless,
            lossless_then_lossy=lossless_then_lossy,
            ccr_inject_marker=ccr,
        )
    )
    calls: list[str] = []

    def _fake_kompress(content, context, question=None):
        calls.append(content)
        out = kompress(content) if kompress else content
        return out, len(out.split())

    r._try_ml_compressor = _fake_kompress  # type: ignore[method-assign]
    return r, calls


def _run(r, content):
    tr, rc = [], {}
    out, was = r._compress_block_content(
        content, hash(content), "", 1.0, 1.0, None, tr, rc, [],
        "tool_result", "tool", True,
    )
    return out, was, tr, rc


def test_a7_chains_fold_then_kompress_when_lossy_helps():
    block = _grep_block()
    # kompress removes >=10% more tokens than the fold → lossy result is kept.
    r, calls = _router(lossless_then_lossy=True, kompress=lambda c: "TINY")
    out, was, tr, rc = _run(r, block)
    assert was is True
    assert out == "TINY"
    assert len(calls) == 1  # kompress ran on the folded remainder
    assert rc.get("lossless_then_lossy_accept") == 1
    assert rc.get("lossless_accept", 0) == 0
    assert tr == ["router:tool_result:lossless_search+kompress"]


def test_a7_keeps_pure_fold_when_lossy_marginal():
    block = _grep_block()
    # kompress returns the fold unchanged (no gain) → pure byte-exact fold kept.
    r, calls = _router(lossless_then_lossy=True, kompress=lambda c: c)
    out, was, tr, rc = _run(r, block)
    assert was is True
    assert len(calls) == 1  # kompress was attempted...
    assert rc.get("lossless_accept") == 1  # ...but the pure fold won
    assert rc.get("lossless_then_lossy_accept", 0) == 0
    assert search_unheading(out) == block  # fully recoverable (byte-exact)


def test_a7_never_kompresses_diff():
    block = _diff_block()
    # kompress would mangle a diff; A7 must never call it on diff content.
    r, calls = _router(lossless_then_lossy=True, kompress=lambda c: "MANGLED")
    out, was, tr, rc = _run(r, block)
    assert was is True
    assert calls == []  # lossy stage never touched the diff
    assert "MANGLED" not in out
    assert out.count("@@ ") == block.count("@@ ")  # every hunk header preserved
    assert "new_foo = 2" in out  # hunk bodies intact → still applies


def test_a7_off_is_a6_pure_fold():
    block = _grep_block()
    r, calls = _router(lossless_then_lossy=False, kompress=lambda c: "TINY")
    out, was, tr, rc = _run(r, block)
    assert was is True
    assert calls == []  # no lossy pass when A7 disabled
    assert rc.get("lossless_accept") == 1
    assert search_unheading(out) == block


def test_a7_never_worse_than_a6():
    block = _grep_block()
    r6, _ = _router(lossless_then_lossy=False, kompress=lambda c: "TINY")
    r7, _ = _router(lossless_then_lossy=True, kompress=lambda c: "TINY")
    out6, _, _, _ = _run(r6, block)
    out7, _, _, _ = _run(r7, block)
    assert len(out7) <= len(out6)  # A7 >= A6 compression, always


def test_a7_noop_in_lossless_only_mode():
    block = _grep_block()
    # lossless-only mode ignores A7 (never emits lossy) even if the flag is set.
    r, calls = _router(lossless_then_lossy=True, lossless=True, kompress=lambda c: "TINY")
    out, was, tr, rc = _run(r, block)
    assert was is True
    assert calls == []  # no lossy in lossless-only mode
    assert search_unheading(out) == block
