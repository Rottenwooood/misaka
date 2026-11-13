"""Token, cost and wall-clock usage read from the board's own ledgers.

Every metered model request already writes a ``budget_usage`` event with its
provider, model and token counts; every attempt already writes a ``task_runs``
row with its start and end. This module only aggregates those two ledgers, so it
adds no accounting of its own and cannot disagree with the cap.
"""
from __future__ import annotations

import json
import time

CURRENCY = "USD"
_MILLION = 1_000_000.0


def _registry():
    try:
        from misaka.core.auth_storage import AuthStorage
        from misaka.core.model_registry import ModelRegistry

        return ModelRegistry.create(AuthStorage.create())
    except Exception:  # noqa: BLE001 - pricing is optional; tokens still report
        return None


def _number(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _token_parts(usage):
    """Read the provider's normalised usage object without assuming one spelling."""
    if not isinstance(usage, dict):
        return 0, 0, 0, 0
    def pick(*names):
        for name in names:
            if name in usage:
                return _number(usage[name])
        return 0
    return (pick("input", "input_tokens", "prompt_tokens"),
            pick("output", "output_tokens", "completion_tokens"),
            pick("cacheRead", "cache_read", "cache_read_input_tokens"),
            pick("cacheWrite", "cache_write", "cache_creation_input_tokens"))


def _price(registry, provider, model):
    if registry is None or not provider or not model:
        return None
    try:
        found = registry.find(str(provider), str(model))
    except Exception:  # noqa: BLE001 - an unknown provider is simply unpriced
        return None
    return getattr(found, "cost", None) if found is not None else None


def _event_rows(con, task_id, generation=None):
    sql = "SELECT payload,created_at FROM events WHERE task_id=? AND kind='budget_usage'"
    params = [task_id]
    if generation is not None:
        sql += " AND generation=?"
        params.append(int(generation))
    return con.execute(sql + " ORDER BY id", params).fetchall()


def _run_rows(con, task_id):
    return con.execute(
        "SELECT started_at,completed_at,status,failure_kind FROM task_runs "
        "WHERE task_id=? ORDER BY started_at,attempt",
        (task_id,),
    ).fetchall()


def _card_usage(con, row, registry, generation=None):
    tokens = {"in": 0, "out": 0, "total": 0}
    amount = 0.0
    priced = True
    model = None
    calls = 0
    events = _event_rows(con, row["id"], generation)
    for event in events:
        try:
            payload = json.loads(event["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        calls += 1
        usage = payload.get("usage") or {}
        tin, tout, tread, twrite = _token_parts(usage)
        total = _number(payload.get("totalTokens")) or tin + tout + tread + twrite
        tokens["in"] += tin
        tokens["out"] += tout
        tokens["total"] += total
        model = payload.get("model") or model
        cost = _price(registry, payload.get("provider"), payload.get("model"))
        if cost is None:
            priced = False
        else:
            amount += (tin * cost.input + tout * cost.output
                       + tread * cost.cacheRead + twrite * cost.cacheWrite) / _MILLION
    runs = _run_rows(con, row["id"])
    now = int(time.time())
    wall = sum(max(0, int(run["completed_at"] or now) - int(run["started_at"])) for run in runs)
    failures = sum(1 for run in runs if run["failure_kind"])
    started = min((int(run["started_at"]) for run in runs), default=row["started_at"])
    return {
        "model": model,
        "tokens": tokens,
        "cost": {"amount": (round(amount, 6) if priced and events else
                            (0.0 if not events else None)), "currency": CURRENCY},
        "wall_time_s": wall,
        "model_calls": calls,
        "attempts": len(runs),
        "failures": failures,
        "started_at": started,
        "finished_at": row["completed_at"],
    }


def _summary(parts):
    tokens = {"in": 0, "out": 0, "total": 0}
    amount = 0.0
    priced = True
    wall = 0
    errors = 0
    calls = 0
    for part in parts:
        for key in tokens:
            tokens[key] += part["tokens"][key]
        if part["cost"]["amount"] is None:
            priced = False
        else:
            amount += part["cost"]["amount"]
        wall += part["wall_time_s"]
        errors += part["failures"]
        calls += part["model_calls"]
    return {
        "tokens": tokens,
        "cost": {"amount": round(amount, 6) if priced else None, "currency": CURRENCY},
        "wall_time_s": wall,
        "model_calls": calls,
        "errors": errors,
    }


def _cards(con, *, card_id=None, request_id=None, instance_id=None):
    sql = "SELECT * FROM card_state WHERE 1=1"
    params = []
    if card_id is not None:
        sql += " AND id=?"
        params.append(card_id)
    if request_id is not None:
        sql += " AND request_id=?"
        params.append(request_id)
    if instance_id is not None:
        sql += " AND instance_id=?"
        params.append(instance_id)
    return con.execute(sql + " ORDER BY id", params).fetchall()


def report(con, *, card_id=None, request_id=None, instance_id=None, live_only=False):
    """One shape, filtered by the caller: a card, a request, an instance or the board."""
    if card_id is not None and con.execute(
            "SELECT 1 FROM card_state WHERE id=?", (card_id,)).fetchone() is None:
        raise ValueError(f"Card not found: {card_id}")
    if request_id is not None and con.execute(
            "SELECT 1 FROM work_requests WHERE id=?", (request_id,)).fetchone() is None:
        raise ValueError(f"Work request not found: {request_id}")
    if instance_id is not None and con.execute(
            "SELECT 1 FROM instances WHERE id=?", (instance_id,)).fetchone() is None:
        raise ValueError(f"Instance not found: {instance_id}")
    registry = _registry()
    if live_only:
        sql = "SELECT * FROM card_state WHERE status='running'"
        params = []
        for column, value in (("id", card_id), ("request_id", request_id),
                              ("instance_id", instance_id)):
            if value is not None:
                sql += f" AND {column}=?"
                params.append(value)
        rows = con.execute(sql + " ORDER BY started_at,id", params).fetchall()
        now = int(time.time())
        live = []
        for row in rows:
            usage = _card_usage(con, row, registry, int(row["generation"]))
            leased = con.execute(
                "SELECT COALESCE(SUM(tokens),0) FROM budget_reservations "
                "WHERE task_id=? AND expires_at>=?",
                (row["id"], now),
            ).fetchone()[0]
            live.append({
                "card_id": row["id"],
                "instance": row["instance_id"],
                "elapsed_s": max(0, now - int(row["started_at"] or now)),
                "tokens_settled": usage["tokens"]["total"],
                "tokens_leased": int(leased or 0),
                "cost_so_far": usage["cost"],
            })
        return live
    rows = _cards(con, card_id=card_id, request_id=request_id, instance_id=instance_id)
    parts = [{"id": row["id"], **_card_usage(con, row, registry)} for row in rows]
    if card_id is not None:
        part = parts[0]
        return {"card_id": part["id"], **{key: part[key] for key in (
            "model", "tokens", "cost", "wall_time_s", "model_calls",
            "attempts", "failures", "started_at", "finished_at")}}
    if request_id is not None or instance_id is not None:
        summary = _summary(parts)
        result = {
            ("request_id" if request_id is not None else "instance_id"):
                request_id if request_id is not None else instance_id,
            "cards": len(parts),
            **summary,
            "by_card": [{
                "card_id": part["id"],
                "tokens": part["tokens"],
                "cost": part["cost"],
                "wall_time_s": part["wall_time_s"],
            } for part in parts],
        }
        return result
    by_instance = {}
    by_model = {}
    for row, part in zip(rows, parts):
        by_instance.setdefault(row["instance_id"], []).append(part)
        if not part["model_calls"]:
            continue
        model = part["model"] or "unknown"
        slot = by_model.setdefault(model, {
            "model": model, "tokens": {"in": 0, "out": 0, "total": 0},
            "calls": 0, "_amount": 0.0, "_priced": True,
        })
        for key in slot["tokens"]:
            slot["tokens"][key] += part["tokens"][key]
        slot["calls"] += part["model_calls"]
        if part["cost"]["amount"] is None:
            slot["_priced"] = False
        else:
            slot["_amount"] += part["cost"]["amount"]
    models = []
    for slot in by_model.values():
        models.append({
            "model": slot["model"],
            "tokens": slot["tokens"],
            "cost": {"amount": round(slot["_amount"], 6) if slot["_priced"] else None,
                     "currency": CURRENCY},
            "calls": slot["calls"],
        })
    summary = _summary(parts)
    return {
        "total": {"tokens": summary["tokens"], "cost": summary["cost"],
                  "wall_time_s": summary["wall_time_s"], "cards": len(parts)},
        "by_instance": [
            {"instance": key, "cards": len(value), **_summary(value)}
            for key, value in sorted(by_instance.items())
        ],
        "by_model": sorted(models, key=lambda item: item["tokens"]["total"], reverse=True),
    }
