"""A fresh home can be given its first Last Order without hand-written JSON."""
from contextlib import closing

from tests.work_fixture import Scenario


def test_bootstrap_creates_a_template_and_an_instance_once():
    s = Scenario()
    try:
        from misaka.core.platform import tasks
        s.cli("state", "bootstrap", "--model", "test/model")
        s.cli("state", "bootstrap", "--model", "test/model")
        with closing(tasks.connect(s.database)) as con:
            templates = con.execute("SELECT id,revision,role FROM templates").fetchall()
            instances = con.execute("SELECT id,template_id FROM instances").fetchall()
            config = con.execute("SELECT config_json FROM templates").fetchone()[0]
        assert [tuple(row) for row in templates] == [("last-order", 1, "last-order")]
        assert [tuple(row) for row in instances] == [("lo", "last-order")]
        assert "test/model" in config
    finally:
        s.close()


def test_setup_leaves_one_coordinator_behind(tmp_path, monkeypatch):
    from contextlib import closing

    from misaka.cli import setup as setup_cli
    from misaka.config import home
    from misaka.core.platform import tasks

    monkeypatch.setattr(home, "path", lambda key: tmp_path / "board.db" if key == "db" else tmp_path / key)
    setup_cli._ensure_last_order()
    setup_cli._ensure_last_order()
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        rows = con.execute("SELECT i.id,t.role FROM instances i JOIN templates t "
                           "ON t.id=i.template_id AND t.revision=i.template_revision").fetchall()
    assert [tuple(row) for row in rows] == [("lo", "last-order")]
