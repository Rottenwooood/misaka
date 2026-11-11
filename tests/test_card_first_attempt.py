"""Coordinator messages use daemon ownership for first attempts and continuations."""
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from misaka.core.network import messages, sister_runtime
from misaka.core.platform import tasks
from tests.factories import contract_card


@pytest.fixture
def board(tmp_path):
    with closing(tasks.connect(str(tmp_path / "board.db"))) as con:
        tid = contract_card(con, str(tmp_path), "Never ran", "## acceptance criteria\nReturn a result", "10032")
        con.execute("UPDATE tasks SET status='stopped' WHERE id=?", (tid,))
        con.commit()
        yield con, tid


def runtime(con):
    rt = sister_runtime.SisterRuntime(None, lambda: con)
    rt._request = AsyncMock(return_value={"pane_id": "worker-pane"})
    return rt


async def test_a_never_ran_card_is_sent_to_the_daemon_with_its_note(board):
    con, tid = board
    rt = runtime(con)
    out = await rt.message(tid, "begin with volume one", summary="v1", confirmed=True,
                           expected_generation=1, context=None)
    assert out["mode"] == "first-attempt"
    rt._request.assert_awaited_once_with("pane.continue_card", {
        "task_id": tid, "say": "begin with volume one", "expected_generation": 1})
    # A launch receipt is not a fabricated status update. Only the daemon owns its claim.
    assert tasks.get(con, tid)["status"] == "stopped"


async def test_starting_a_never_ran_card_needs_the_users_word(board):
    con, tid = board
    rt = runtime(con)
    with pytest.raises(ValueError, match="confirmation is required"):
        await rt.message(tid, "go", summary="go", confirmed=False, expected_generation=1, context=None)
    rt._request.assert_not_awaited()


async def test_a_stale_generation_is_refused_before_anything_starts(board):
    con, tid = board
    rt = runtime(con)
    with pytest.raises(ValueError, match="changed"):
        await rt.message(tid, "go", summary="go", confirmed=True, expected_generation=9, context=None)
    rt._request.assert_not_awaited()


async def test_running_note_targets_the_instance_and_exact_attempt(board, tmp_path, monkeypatch):
    con, tid = board
    row = tasks.get(con, tid)
    con.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    con.commit()
    rt = runtime(con)
    real_connect = messages.connect
    monkeypatch.setattr(messages, "connect", lambda path=None: real_connect(str(tmp_path / "messages.db")))
    await rt.message(tid, "the note", summary="note", confirmed=False, expected_generation=1, context=None)
    with closing(real_connect(str(tmp_path / "messages.db"))) as mail:
        rows = messages.pending(mail, row["instance_id"], task_id=tid)
    assert len(rows) == 1 and rows[0]["generation"] == 1 and rows[0]["body"] == "the note"
    rt._request.assert_not_awaited()
