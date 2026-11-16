"""OpenCode's per-conversation routing header, translated from pi's
``providers/opencode-headers.ts``."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

OPENCODE_SESSION_HEADER = "x-opencode-session"
_HEADER_SAFE = re.compile(r"^[\x21-\x7e]+$")


def _has_header(headers: Mapping[str, Any] | None, name: str) -> bool:
    expected = name.lower()
    return any(key.lower() == expected for key in (headers or {}))


def _read(options: Any, name: str) -> Any:
    if options is None:
        return None
    if isinstance(options, Mapping):
        return options.get(name)
    return getattr(options, name, None)


def _model_field(model: Any, name: str) -> str:
    value = getattr(model, name, None)
    if value is None and isinstance(model, Mapping):
        value = model.get(name)
    return str(value or "")


def is_opencode_model(model: Any) -> bool:
    """Whether this model reaches OpenCode's gateway, whatever its route name."""
    if _model_field(model, "provider") in ("opencode", "opencode-go"):
        return True
    try:
        host = urlparse(_model_field(model, "baseUrl")).hostname or ""
    except ValueError:
        return False
    return host == "opencode.ai" or host.endswith(".opencode.ai")


def _session_id(options: Any) -> str:
    value = _read(options, "sessionId")
    text = str(value) if value is not None else ""
    # The gateway needs an opaque, stable per-conversation value. A caller with no
    # session gets a fresh one; a malformed id must not become an invalid header.
    return text if _HEADER_SAFE.fullmatch(text) else uuid.uuid4().hex


def _with_session_header(model: Any, options: Any) -> Any:
    if not is_opencode_model(model):
        return options
    headers = _read(options, "headers") or {}
    if _has_header(headers, OPENCODE_SESSION_HEADER):
        return options
    headers = {**headers, OPENCODE_SESSION_HEADER: _session_id(options)}
    if options is None:
        return {"headers": headers}
    if isinstance(options, Mapping):
        return {**options, "headers": headers}
    # Options arrive as pydantic models from the runtime and as plain mappings from
    # callers; a model is copied the way `{...options}` copies an object.
    if hasattr(options, "model_copy"):
        return options.model_copy(update={"headers": headers})
    return {**vars(options), "headers": headers}


class _WithOpenCodeSessionHeader:
    def __init__(self, streams: Any) -> None:
        self._streams = streams

    def stream(self, model, context, options=None):
        return self._streams.stream(model, context, _with_session_header(model, options))

    def streamSimple(self, model, context, options=None):
        return self._streams.streamSimple(model, context, _with_session_header(model, options))


def with_opencode_session_header(streams: Any) -> _WithOpenCodeSessionHeader:
    """Adds OpenCode's required per-conversation routing header before API dispatch."""
    return _WithOpenCodeSessionHeader(streams)


withOpenCodeSessionHeader = with_opencode_session_header

__all__ = [
    "OPENCODE_SESSION_HEADER",
    "is_opencode_model",
    "withOpenCodeSessionHeader",
    "with_opencode_session_header",
]
