"""A copy reports success only when a clipboard backend actually took the text."""
from misaka.utils import clipboard


def test_the_linux_command_chain_is_used_before_the_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(clipboard, "_copy_via_command", lambda name, text: calls.append((name, text)) or True)
    monkeypatch.setattr(clipboard, "emit_osc52", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no OSC 52")))
    assert clipboard.copy_text_sync("hello", platform_name="linux") is True
    assert calls == [("linux", "hello")]


def test_a_local_osc52_write_is_not_evidence(monkeypatch):
    monkeypatch.setattr(clipboard, "_copy_via_command", lambda name, text: False)
    monkeypatch.setattr(clipboard, "emit_osc52", lambda text, writer=None: True)
    monkeypatch.setattr(clipboard, "is_remote_session", lambda env=None: False)
    assert clipboard.copy_text_sync("hello", platform_name="linux") is False


def test_a_remote_osc52_write_counts(monkeypatch):
    monkeypatch.setattr(clipboard, "_copy_via_command", lambda name, text: False)
    monkeypatch.setattr(clipboard, "emit_osc52", lambda text, writer=None: True)
    monkeypatch.setattr(clipboard, "is_remote_session", lambda env=None: True)
    assert clipboard.copy_text_sync("hello", platform_name="linux") is True


def test_the_async_copy_raises_when_only_a_local_osc52_was_written(monkeypatch):
    import asyncio
    monkeypatch.setattr(clipboard, "_get_native_clipboard_backend", lambda: None)
    monkeypatch.setattr(clipboard, "_copy_via_command", lambda name, text: False)
    monkeypatch.setattr(clipboard, "emit_osc52", lambda text, writer=None: True)
    monkeypatch.setattr(clipboard, "is_remote_session", lambda env=None: False)
    async def run():
        try:
            await clipboard.copy_to_clipboard("hello")
        except RuntimeError as error:
            return str(error)
        raise AssertionError("a write that nobody confirmed must not report success")
    assert "install xclip" in asyncio.run(run())


def test_the_panel_copy_uses_the_shared_chain(monkeypatch):
    from misaka.ui.panel import panel as panel_mod
    from misaka.utils import clipboard as shared

    seen = []
    monkeypatch.setattr(shared, "copy_text_sync", lambda text: seen.append(text) or False)
    assert panel_mod._clipboard("hello") is False
    assert seen == ["hello"]
