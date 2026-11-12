"""A delivery reaches the card it names, and only that card."""
from misaka.core.messaging import delivery
from misaka.core.network import messages


def item(content, message_id="m-1"):
    return {"id": message_id, "type": "test.failed", "content": content, "source": "tests"}


def test_a_named_card_receives_the_message(monkeypatch):
    calls = {}
    monkeypatch.setattr(messages, "resolve_address",
                        lambda addr, **kw: calls.setdefault("target", {"addr": addr}))
    monkeypatch.setattr(messages, "post",
                        lambda target, **kw: calls.setdefault("post", kw) and 7)
    assert delivery.deliver([item({"card": "t_a00001"})]) == ["m-1"]
    assert calls["target"] == {"addr": "t_a00001"}
    assert "test.failed" in calls["post"]["summary"]
    assert "t_a00001" in calls["post"]["body"] or "test.failed" in calls["post"]["body"]


def test_a_card_id_inside_text_is_enough(monkeypatch):
    seen = {}
    monkeypatch.setattr(messages, "resolve_address",
                        lambda addr, **kw: seen.setdefault("addr", addr) or {})
    monkeypatch.setattr(messages, "post", lambda target, **kw: 1)
    assert delivery.deliver([item("card t_b00002 failed")]) == ["m-1"]
    assert seen["addr"] == "t_b00002"


def test_a_delivery_naming_no_card_is_dropped(monkeypatch):
    monkeypatch.setattr(messages, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert delivery.deliver([item({"note": "nothing"})]) == ["m-1"]


def test_a_message_the_card_cannot_take_is_dropped(monkeypatch):
    monkeypatch.setattr(messages, "resolve_address",
                        lambda addr, **kw: (_ for _ in ()).throw(ValueError("not live")))
    assert delivery.deliver([item({"card": "t_c00003"})]) == ["m-1"]
