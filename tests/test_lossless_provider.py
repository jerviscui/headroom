"""The pluggable lossless-provider seam + its verify-and-revert safety gate."""

import json

import pytest

from headroom.transforms.lossless_provider import (
    Compaction,
    LosslessCtx,
    best_provider_fold,
    clear_lossless_providers,
    register_lossless_provider,
    registered_lossless_providers,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_lossless_providers()
    yield
    clear_lossless_providers()


def test_no_providers_is_noop():
    # Default OSS behaviour: no providers registered → seam is inert.
    assert registered_lossless_providers() == []
    assert best_provider_fold("some long content here", LosslessCtx()) is None


def test_verified_byte_provider_wins():
    class P:
        name = "p"

        def propose(self, content, ctx):
            return Compaction(text="X", recover=lambda: content, label="lossless_p")

    register_lossless_provider(P())
    assert best_provider_fold("a much longer original string", LosslessCtx()) == ("X", "lossless_p")


def test_provider_failing_verification_is_discarded():
    # recover() does NOT reproduce the input → the core rejects it (no loss ships).
    class Bad:
        name = "bad"

        def propose(self, content, ctx):
            return Compaction(text="X", recover=lambda: "WRONG", label="lossless_bad")

    register_lossless_provider(Bad())
    assert best_provider_fold("original content here", LosslessCtx()) is None


def test_provider_that_raises_is_skipped():
    class Boom:
        name = "boom"

        def propose(self, content, ctx):
            raise RuntimeError("nope")

    register_lossless_provider(Boom())
    assert best_provider_fold("original content here", LosslessCtx()) is None


def test_json_equivalence_accepts_reformat_rejects_mutation():
    pretty = '{\n  "a": 1,\n  "b": [1, 2, 3]\n}'

    class Good:
        name = "good"

        def propose(self, content, ctx):
            compact = json.dumps(json.loads(content), separators=(",", ":"))
            return Compaction(
                text=compact, recover=lambda: compact, equivalence="json", label="lossless_good"
            )

    class Liar:
        name = "liar"

        def propose(self, content, ctx):
            # claims json-equivalence but drops a field → must be rejected
            return Compaction(
                text='{"a":1}', recover=lambda: '{"a":1}', equivalence="json", label="lossless_liar"
            )

    register_lossless_provider(Good())
    out = best_provider_fold(pretty, LosslessCtx())
    assert out is not None and out[1] == "lossless_good" and len(out[0]) < len(pretty)

    clear_lossless_providers()
    register_lossless_provider(Liar())
    assert best_provider_fold(pretty, LosslessCtx()) is None  # value mutation rejected
