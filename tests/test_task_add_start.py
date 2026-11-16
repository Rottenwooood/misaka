"""CLI cards require explicit request and instance identities; starts use the canonical card."""

from contextlib import closing

import pytest

from misaka.cli import app
from misaka.core.network import dispatch, roster
from misaka.core.platform import tasks
from tests.factories import identity

CONTRACT = "## goal\nList yards.\n## boundaries\nLocal only.\n## acceptance criteria\n- A checked table.\n"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(roster, "roster_names", lambda root=None: ["10032", "10077"])
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        rid, iid = identity(con, "10032", str(tmp_path))
        monkeypatch.setattr(app.db, "connect", lambda path: con)
        yield con, rid, iid


def command(*args):
    return app._cmd_task(app._parser().parse_args(["task", *args]))


def test_create_start_and_delete(board, monkeypatch, tmp_path, capsys):
    con, rid, iid = board
    (tmp_path / "contract.md").write_text(CONTRACT)
    with pytest.raises(SystemExit) as result:
        command(
            "add",
            "Salt",
            "yards",
            "--request",
            rid,
            "--instance",
            iid,
            "--body-file",
            "contract.md",
            "--reviewer",
            "10077",
        )
    assert result.value.code == 0
    [row] = tasks.by_status(con, "ready")
    assert (row["request_id"], row["instance_id"], row["assignee"]) == (
        rid,
        iid,
        "10032",
    )
    assert not (tmp_path / "cards" / (row["id"] + ".md")).exists()
    ran = []
    monkeypatch.setattr(
        dispatch, "run_task", lambda con, row, cfg: ran.append(row["id"]) or True
    )
    with pytest.raises(SystemExit) as result:
        command("start", row["id"])
    assert result.value.code == 0 and ran == [row["id"]]
    with pytest.raises(SystemExit) as result:
        command(row["id"], "--delete")
    assert result.value.code == 0 and tasks.get(con, row["id"]) is None


@pytest.mark.parametrize(
    "case,said",
    [
        ("instance", "Instance not found"),
        ("request", "Work request not found"),
        ("reviewer", "different from the assignee"),
    ],
)
def test_invalid_contract_or_identity_is_refused(board, case, said):
    con, rid, iid = board
    args = [
        "add",
        "Test",
        "--request",
        "missing" if case == "request" else rid,
        "--instance",
        "missing" if case == "instance" else iid,
        "--body",
        CONTRACT,
    ]
    if case == "reviewer":
        args += ["--reviewer", "10032"]
    with pytest.raises(SystemExit) as result:
        command(*args)
    assert said in str(result.value.code)
    assert not tasks.by_status(con, "ready")


def test_body_file_encoding(board, tmp_path):
    _con, rid, iid = board
    (tmp_path / "bom.md").write_bytes(b"\xef\xbb\xbf" + CONTRACT.encode())
    with pytest.raises(SystemExit) as result:
        command(
            "add", "BOM", "--request", rid, "--instance", iid, "--body-file", "bom.md"
        )
    assert result.value.code == 0
    (tmp_path / "gbk.md").write_bytes("列出盐场".encode("gbk"))
    with pytest.raises(SystemExit) as result:
        command(
            "add", "GBK", "--request", rid, "--instance", iid, "--body-file", "gbk.md"
        )
    assert "not UTF-8" in str(result.value.code)
