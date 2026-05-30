from headroom.engine.contract import (
    Flavor,
    Provider,
    RequestContext,
    RequestDecision,
    ResponseTelemetry,
    StreamContext,
)


def test_request_context_carries_fields():
    ctx = RequestContext(
        provider=Provider.ANTHROPIC,
        flavor=Flavor.MESSAGES,
        headers_view={"x-api-key": "redacted"},
        raw_body=b'{"model":"claude"}',
        session_key="abc123",
    )
    assert ctx.provider is Provider.ANTHROPIC
    assert ctx.flavor is Flavor.MESSAGES
    assert ctx.raw_body == b'{"model":"claude"}'
    assert ctx.session_key == "abc123"
    assert ctx.headers_view["x-api-key"] == "redacted"


def test_request_decision_defaults_to_passthrough_telemetry():
    body = b'{"x":1}'
    dec = RequestDecision(body=body, telemetry=ResponseTelemetry())
    assert dec.body is body
    assert dec.telemetry.compressed is False
    assert dec.telemetry.bytes_saved == 0
    assert dec.notes == {}


def test_stream_context_is_per_stream_handle():
    sc = StreamContext(session_key="k", provider=Provider.OPENAI, flavor=Flavor.CHAT)
    sc.state["seen_done"] = False
    assert sc.state["seen_done"] is False
