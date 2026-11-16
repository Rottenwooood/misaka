"""The OpenCode gateway requires an opaque session id on every request."""
from types import SimpleNamespace

from misaka.ai.providers.opencode_headers import (
    OPENCODE_SESSION_HEADER,
    with_opencode_session_header,
)
from misaka.ai.types import SimpleStreamOptions


class Streams:
    def __init__(self):
        self.options = None

    def streamSimple(self, model, context, options=None):
        self.options = options
        return options


def model(provider="opencode-go", base_url="https://opencode.ai/zen/go/v1"):
    return SimpleNamespace(provider=provider, baseUrl=base_url, api="openai-completions")


def test_an_existing_session_id_is_the_routing_header():
    streams = Streams()
    with_opencode_session_header(streams).streamSimple(
        model(), None, SimpleStreamOptions(sessionId="session-123"))
    assert streams.options.headers[OPENCODE_SESSION_HEADER] == "session-123"


def test_a_direct_call_without_a_session_gets_a_fresh_opaque_id():
    streams = Streams()
    with_opencode_session_header(streams).streamSimple(model(), None, SimpleStreamOptions())
    value = streams.options.headers[OPENCODE_SESSION_HEADER]
    assert value and value != "None"


def test_a_caller_header_wins():
    streams = Streams()
    with_opencode_session_header(streams).streamSimple(
        model(), None,
        SimpleStreamOptions(sessionId="session-123",
                            headers={OPENCODE_SESSION_HEADER: "explicit"}))
    assert streams.options.headers[OPENCODE_SESSION_HEADER] == "explicit"


def test_other_providers_are_untouched():
    options = SimpleStreamOptions()
    streams = Streams()
    with_opencode_session_header(streams).streamSimple(
        model("deepseek", "https://api.deepseek.com"), None, options)
    assert streams.options is options


def test_the_gateway_host_alone_is_enough():
    streams = Streams()
    with_opencode_session_header(streams).streamSimple(
        model("custom", "https://opencode.ai/zen/go/v1"),
        None, SimpleStreamOptions(sessionId="session-456"))
    assert streams.options.headers[OPENCODE_SESSION_HEADER] == "session-456"


def test_the_direct_stream_seam_also_adds_the_header(monkeypatch):
    import importlib

    stream_module = importlib.import_module("misaka.ai.stream")
    from misaka.ai.types import Context

    captured = {}

    class API:
        def streamSimple(self, model, context, options=None):
            captured["options"] = options
            return "stream"

    monkeypatch.setattr(stream_module, "get_api_provider", lambda api: API())
    assert stream_module.stream_simple(
        model(), Context(messages=[]), SimpleStreamOptions()) == "stream"
    assert captured["options"].headers[OPENCODE_SESSION_HEADER]


def test_no_options_object_still_gets_a_header():
    streams = Streams()
    with_opencode_session_header(streams).streamSimple(model(), None, None)
    assert streams.options["headers"][OPENCODE_SESSION_HEADER]


def test_a_malformed_session_id_falls_back_to_a_header_safe_id():
    streams = Streams()
    with_opencode_session_header(streams).streamSimple(
        model(), None, SimpleStreamOptions(sessionId="bad\r\nX-Injected: 1"))
    value = streams.options.headers[OPENCODE_SESSION_HEADER]
    assert value.isascii() and "\r" not in value and "\n" not in value
