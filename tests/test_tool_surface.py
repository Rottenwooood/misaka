"""Tool surfaces: names a person would guess, no duplicate or constant fields."""
import json
import sqlite3
from contextlib import closing

from tests.work_fixture import BODY, Scenario


def test_creating_instances_without_changes_does_not_grow_the_tree():
    s = Scenario()
    try:
        s.apply([{"op": "template.create", "id": "sister", "role": "sister", "config": {}}])
        for index in range(6):
            s.apply([{"op": "instance.create", "id": f"w{index}", "template_id": "sister",
                      "template_revision": 1}])
        with closing(sqlite3.connect(s.database)) as con:
            revisions = con.execute("SELECT revision FROM templates WHERE id='sister'").fetchall()
        assert revisions == [(1,)]
    finally:
        s.close()


def test_a_patch_derives_exactly_one_node():
    s = Scenario()
    try:
        s.apply([{"op": "template.create", "id": "sister", "role": "sister",
                  "config": {"system": "base"}}])
        s.apply([{"op": "instance.create", "id": "w1", "template_id": "sister",
                  "template_revision": 1, "config": {"model": "x/y"}}])
        with closing(sqlite3.connect(s.database)) as con:
            rows = con.execute("SELECT revision,parent_revision FROM templates ORDER BY revision").fetchall()
        assert rows == [(1, None), (2, 1)]
    finally:
        s.close()


def test_a_card_reads_back_the_lists_it_saved():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order", "config": {}},
            {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
            {"op": "template.create", "id": "sister", "role": "sister", "config": {}},
            {"op": "instance.create", "id": "worker", "template_id": "sister", "template_revision": 1},
            {"op": "request.create", "id": "req", "title": "R", "owner_id": "lo",
             "workspace": str(s.project)},
        ])
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{
                "id": "t_a00001", "title": "One", "instance_id": "worker", "body": BODY,
                "fs_scope": ["a"], "forbidden_commands": ["git push"],
                "subscriptions": [{"source": "tests", "type": "test.failed"}]}])
            state = work.owned_state(con, "lo", "req")
        card = state["cards"][0]
        assert card["fs_scope"] == ["a"]
        assert card["forbidden_commands"] == ["git push"]
        assert card["subscriptions"] == [{"source": "tests", "type": "test.failed", "target": None}]
        assert card["instance"] == "worker"
        assert "priority" not in card and "reviewer" not in card
    finally:
        s.close()


def test_the_tool_schemas_name_one_thing_once():
    from misaka.core.network.messages import AskParams, SendMessageParams
    from misaka.core.network.wiring import coordination as tools

    assert set(SendMessageParams.model_fields) == {"to", "message"}
    assert set(AskParams.model_fields) == {"to", "message"}
    card = tools.PlanCard.model_fields
    assert "instance" in card and "instance_id" not in card
    assert "priority" not in card and "reviewer" not in card and "model" not in card
    assert tools.DispatchCard.model_fields["generation"].is_required() is False
    assert "role" not in tools.CreateInstance.model_fields or (
        tools.CreateInstance.model_fields["role"].is_required() is False)


def test_a_shell_does_not_inherit_credentials(monkeypatch):
    from misaka.core.tools import bash

    monkeypatch.setenv("KIMI_API_KEY", "secret-value")
    monkeypatch.setenv("MISAKA_CENTER_TOKEN", "another-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    context = bash._resolve_spawn_context("env", "/tmp", None, False, None)
    assert "KIMI_API_KEY" not in context.env
    assert "MISAKA_CENTER_TOKEN" not in context.env
    assert context.env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert "PATH" in context.env


def test_saving_without_the_two_path_fields_keeps_them():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order", "config": {}},
            {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
            {"op": "template.create", "id": "sister", "role": "sister", "config": {}},
            {"op": "instance.create", "id": "worker", "template_id": "sister", "template_revision": 1},
            {"op": "request.create", "id": "req", "title": "R", "owner_id": "lo",
             "workspace": str(s.project)},
        ])
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": "t_a00001", "title": "One", "instance_id": "worker",
                                              "body": BODY, "fs_scope": ["src"]}])
            work.update_plan(con, "req", 1, [{"id": "t_a00001", "title": "One changed"}])
            card = work.owned_state(con, "lo", "req")["cards"][0]
        assert card["fs_scope"] == ["src"] and card["title"] == "One changed"
    finally:
        s.close()


def test_a_blocked_card_can_be_resumed():
    from misaka.core.platform import tasks, work
    s = Scenario()
    try:
        s.apply([
            {"op": "template.create", "id": "lo", "role": "last-order", "config": {}},
            {"op": "instance.create", "id": "lo", "template_id": "lo", "template_revision": 1},
            {"op": "template.create", "id": "sister", "role": "sister", "config": {}},
            {"op": "instance.create", "id": "worker", "template_id": "sister", "template_revision": 1},
            {"op": "request.create", "id": "req", "title": "R", "owner_id": "lo",
             "workspace": str(s.project)},
        ])
        with closing(tasks.connect(s.database)) as con:
            work.update_plan(con, "req", 0, [{"id": "t_a00001", "title": "One",
                                              "instance_id": "worker", "body": BODY}])
            tasks.block_task(con, "t_a00001", "needs_input", "waiting")
            work.resume_card(con, "t_a00001")
            assert tasks.get(con, "t_a00001")["status"] == "ready"
    finally:
        s.close()


def test_deleting_an_unused_instance_and_template():
    from misaka.core.platform import work
    s = Scenario()
    try:
        s.apply([{"op": "template.create", "id": "sister", "role": "sister", "config": {}},
                 {"op": "instance.create", "id": "probe", "template_id": "sister",
                  "template_revision": 1}])
        from misaka.core.platform import tasks
        with closing(tasks.connect(s.database)) as con:
            work.delete_instance(con, "probe")
            work.delete_template(con, "sister")
        assert json.loads(s.cli("state", "show").stdout)["instances"] == []
    finally:
        s.close()


def test_publishing_to_an_unstarted_card_declares_its_route():
    from misaka.core.network import messages

    declared = []

    class Client:
        base_url = "http://127.0.0.1:9999"

        def send(self, *args, **kwargs):
            return {"id": "m-1"}

    client = Client()
    import misaka.core.messaging.config as center_config
    from misaka.core.messaging import registry

    old_center, old_url, old_ensure, old_send = (
        center_config.center, center_config.inbox_url, registry.ensure_endpoint, messages._target_instance)
    try:
        center_config.center = lambda: client
        center_config.inbox_url = lambda: "http://127.0.0.1:9999"
        registry.ensure_endpoint = lambda *args: declared.append(args)
        messages._target_instance = lambda target: "worker"
        messages._ROUTED.clear()
        messages._publish({"to_task": "t_a00001"}, "hello", summary="hello", sender="lo")
        assert declared and declared[0][1:] == ("worker", "http://127.0.0.1:9999")
    finally:
        center_config.center, center_config.inbox_url = old_center, old_url
        registry.ensure_endpoint, messages._target_instance = old_ensure, old_send


def test_a_patch_equal_to_the_node_does_not_create_a_revision():
    s = Scenario()
    try:
        s.apply([{"op": "template.create", "id": "sister", "role": "sister",
                  "config": {"system": "base"}},
                 {"op": "instance.create", "id": "w1", "template_id": "sister",
                  "template_revision": 1, "config": {"system": "base"}}])
        with closing(sqlite3.connect(s.database)) as con:
            assert con.execute("SELECT count(*) FROM templates").fetchone()[0] == 1
            assert con.execute("SELECT template_revision FROM instances WHERE id='w1'").fetchone()[0] == 1
    finally:
        s.close()
