"""Tool surfaces: names a person would guess, no duplicate or constant fields."""
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
