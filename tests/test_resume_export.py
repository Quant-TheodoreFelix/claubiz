import asyncio
import os

import pytest
from test_orchestrator import _setup

from claubiz.budget import BudgetGuard, Ledger, ReservationPool
from claubiz.config import Settings
from claubiz.orchestrator import RunError
from claubiz.paths import home
from claubiz.workspace import Workspace, WorkspaceConfig, WorkspaceError, export_report


async def _cancel_at_t4(orch, fake, session):
    fake.hold = asyncio.Event()
    fake.held = {"t4"}
    task = asyncio.create_task(orch.execute(session))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if ("start", "t4") in fake.order:
            break
    record = orch.ws.load_run(session.run_id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fake.hold = None
    return record


async def test_running_record_is_saved_per_task(tmp_path):
    _ws, fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("조사", None)
    midway = await _cancel_at_t4(orch, fake, session)
    assert midway["status"] == "running"
    done = {t["id"] for t in midway["tasks"] if t["status"] == "done"}
    assert done == {"t1", "t2", "t3"}
    assert orch.ws.load_run(session.run_id)["status"] == "cancelled"


async def test_resume_runs_only_remaining_tasks(tmp_path):
    ws, fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("조사", None)
    await _cancel_at_t4(orch, fake, session)
    spent_before = orch.ledger.spent_run(session.run_id)
    calls_before = len(fake.calls)

    resumed = orch.resume(session.run_id)
    assert resumed.resumed and set(resumed.prior_states) == {"t1", "t2", "t3"}
    assert resumed.estimate["tasks"]["t1"] == 0.0 and resumed.estimate["tasks"]["t4"] > 0
    outcome = await orch.execute(resumed)
    assert outcome.status == "completed"
    new_calls = fake.calls[calls_before:]
    assert {c[1] for c in new_calls} == {"t4", "synth"}
    assert "From t1" in fake.messages[("t4", "medium")]
    record = ws.load_run(session.run_id)
    assert record["status"] == "completed" and record["run_id"] == session.run_id
    assert orch.ledger.spent_run(session.run_id) > spent_before
    assert "t1 노트" in outcome.report_path.read_text(encoding="utf-8") or record["summary"]

    with pytest.raises(RunError):
        orch.resume(session.run_id)


async def test_resume_guards(tmp_path):
    ws, fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("조사", None, allow_confidential=True)
    await _cancel_at_t4(orch, fake, session)
    with pytest.raises(RunError):
        orch.resume(session.run_id)
    resumed = orch.resume(session.run_id, allow_confidential=True)
    orch.cancel_planned(resumed)
    assert ws.load_run(session.run_id)["status"] == "cancelled"

    other = await orch.plan("조사 2", None)
    await _cancel_at_t4(orch, fake, other)
    ws.remove_input("memo.md")
    record = ws.load_run(other.run_id)
    for task in record["tasks"]:
        if task["id"] == "t3":
            task["status"] = "running"
    ws.save_run(other.run_id, record)
    with pytest.raises(RunError, match="memo.md"):
        orch.resume(other.run_id)


async def test_model_switch_applies_to_next_calls(tmp_path):
    ws, fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("조사", None)
    assert {m for _, tid, m in fake.models if tid == "plan"} == {"claude-opus-5-5"}
    fake.hold = asyncio.Event()
    fake.held = {"t4"}
    task = asyncio.create_task(orch.execute(session))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if ("start", "t4") in fake.order:
            break
    orch.set_model("claude-haiku-5-5", session.run_id)
    fake.hold.set()
    outcome = await task
    assert outcome.status == "completed"
    by_task = {tid: m for _, tid, m in fake.models if tid in ("t1", "t4", "synth")}
    assert by_task["t1"] == "claude-opus-5-5"
    assert by_task["synth"] == "claude-haiku-5-5"
    record = ws.load_run(session.run_id)
    assert set(record["models"].values()) == {"claude-haiku-5-5"}
    with pytest.raises(RunError):
        orch.set_model("claude-unknown-9")


async def test_shared_pool_counts_other_runs_against_month_cap():
    ledger = Ledger()
    settings = Settings()
    pool = ReservationPool()
    first = BudgetGuard(ledger, settings, "a", 0.0, "r20261104-000000-aaaaaa", 50.0, pool)
    second = BudgetGuard(ledger, settings, "b", 0.0, "r20261104-000000-bbbbbb", 50.0, pool)
    held = await first.reserve(46.0, "x")
    waiter = asyncio.create_task(second.reserve(46.0, "y"))
    await asyncio.sleep(0.05)
    assert not waiter.done() and second.waiting == 1
    await first.release(held)
    reservation = await asyncio.wait_for(waiter, 1)
    assert pool.total == 46.0 and pool.by_workspace == {"b": 46.0}
    await second.release(reservation)
    assert pool.total == 0.0

    alone = BudgetGuard(ledger, settings, "c", 0.0, "r20261104-000000-cccccc", 50.0)
    other = BudgetGuard(ledger, settings, "d", 0.0, "r20261104-000000-dddddd", 50.0)
    await alone.reserve(46.0, "x")
    await asyncio.wait_for(other.reserve(46.0, "y"), 1)


async def test_import_report_from_other_workspace(tmp_path):
    source, _fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("조사", None)
    await orch.execute(session)
    target = Workspace.create(WorkspaceConfig(name="beta"))
    entry = target.import_run_report(source, session.run_id, "internal")
    assert entry.name == f"acme-{session.run_id}.md" and entry.classification == "internal"
    text = target.read_input(entry)
    assert text.startswith("> 출처: 작업 영역 acme") and "실행 r" not in text.split("\n", 2)[2][-80:]
    with pytest.raises(WorkspaceError):
        target.import_run_report(target, session.run_id, "internal")
    web_only = Workspace.create(WorkspaceConfig(name="web", scope="web-only"))
    with pytest.raises(WorkspaceError):
        web_only.import_run_report(source, session.run_id, "internal")

    secret = await orch.plan("기밀 조사", None, allow_confidential=True)
    await orch.execute(secret)
    with pytest.raises(WorkspaceError, match="confidential"):
        target.import_run_report(source, secret.run_id, "internal")
    assert target.import_run_report(source, secret.run_id, "confidential").classification == "confidential"


async def test_export_report_is_safe(tmp_path):
    ws, _fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("조사", None)
    await orch.execute(session)
    out = tmp_path / "exports"
    out.mkdir()
    first, digest = export_report(ws, session.run_id, str(out))
    second, _ = export_report(ws, session.run_id, str(out) + "/")
    assert first.parent == out.resolve() and first.suffix == ".md" and len(digest) == 64
    assert second.name.endswith("(2).md") and first.read_text(encoding="utf-8") == ws.read_report(session.run_id)
    assert first.stat().st_mode & 0o777 == 0o600

    link = tmp_path / "link"
    link.symlink_to(out)
    for bad in (str(link), str(home()), str(home() / "workspaces"), "relative/dir", str(tmp_path / "missing"),
                str(first)):
        with pytest.raises(WorkspaceError):
            export_report(ws, session.run_id, bad)
    planned = await orch.plan("미실행", None)
    with pytest.raises(WorkspaceError):
        export_report(ws, planned.run_id, str(out))
    assert sorted(p.name for p in out.iterdir()) == sorted([first.name, second.name])
    assert not any(name.startswith(".") for name in os.listdir(out))
