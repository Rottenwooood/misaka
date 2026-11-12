"""Last Order is a role with a configurable tool list; its default does no execution."""
from misaka.core.platform import instances

EXECUTION = {"bash", "powershell", "write", "edit", "office", "read", "grep", "find", "ls"}


def test_last_order_defaults_are_management_and_messages_only():
    defaults = set(instances.default_tools("last-order"))
    assert {"misaka_task", "misaka_roster", "SendMessage", "misaka_sister_peek"} <= defaults
    assert not defaults & EXECUTION


def test_a_sister_starts_with_every_tool_of_its_role():
    assert instances.default_tools("sister") is None
