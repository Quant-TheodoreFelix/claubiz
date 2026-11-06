from pathlib import Path

import pytest
from support import assert_private
from test_orchestrator import FakeLLM, _setup

from claubiz.audit import AuditLog
from claubiz.brief import generate_brief
from claubiz.budget import Ledger
from claubiz.config import Settings
from claubiz.events import EventBus
from claubiz.orchestrator import RunError
from claubiz.paths import UnsafePathError, home, home_path
from claubiz.playbooks import (
    BUILTIN_PLAYBOOKS,
    PlaybookError,
    PlaybookSpec,
    all_playbooks,
    get_playbook,
    load_custom_playbooks,
    playbooks_dir,
    save_custom_playbook,
)
from claubiz.runtime import Runtime
from claubiz.workspace import Workspace, WorkspaceConfig, WorkspaceError


def test_home_path_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUBIZ_HOME", raising=False)
    assert home_path() == Path.home() / ".claubiz"
    monkeypatch.setenv("CLAUBIZ_HOME", str(tmp_path / "data"))
    assert home() == tmp_path / "data"
    assert_private(tmp_path / "data", 0o700)
    monkeypatch.setenv("CLAUBIZ_HOME", "relative/dir")
    with pytest.raises(UnsafePathError):
        home_path()


async def test_delete_run_and_workspace(tmp_path):
    ws, _fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("조사", None)
    await orch.execute(session)
    ledger = Ledger()
    spent = ledger.spent_run(session.run_id)
    assert spent > 0
    assert ws.read_report(session.run_id)
    assert ws.delete_run(session.run_id) is True
    assert ws.load_run(session.run_id) is None
    assert not (ws.root / "outputs" / session.run_id).exists()
    assert ledger.spent_run(session.run_id) == spent
    assert ws.delete_run(session.run_id) is False
    with pytest.raises(WorkspaceError):
        ws.delete_run("../../etc")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (ws.root / "outputs" / "link").symlink_to(outside)
    Workspace.delete("acme")
    assert not ws.root.exists()
    assert (outside / "keep.txt").exists()
    with pytest.raises(WorkspaceError):
        Workspace.delete("acme")


def test_brief_read_write():
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    assert ws.read_brief() == "" and not ws.has_brief()
    assert ws.write_brief("  독자는 영업팀장  ") == len("독자는 영업팀장")
    assert ws.read_brief().strip() == "독자는 영업팀장"
    assert ws.write_brief("") == 0 and not ws.has_brief()
    with pytest.raises(WorkspaceError):
        ws.write_brief("가" * 20001)


def _spec(**overrides):
    data = {
        "id": "partner-scan", "title": "협력사 발굴", "planner_guidance": "업종별로 나눔",
        "synth_guidance": "후보 비교표", "needs_web": True,
    }
    data.update(overrides)
    return PlaybookSpec(**data)


def test_custom_playbooks():
    playbook = save_custom_playbook(_spec())
    assert playbook.builtin is False
    assert get_playbook("partner-scan").title == "협력사 발굴"
    assert set(BUILTIN_PLAYBOOKS) < set(all_playbooks())
    assert_private(playbooks_dir() / "partner-scan.toml", 0o600)
    with pytest.raises(PlaybookError):
        save_custom_playbook(_spec())
    with pytest.raises(PlaybookError):
        save_custom_playbook(_spec(id="market-research"))
    with pytest.raises(ValueError):
        _spec(id="Bad Id")
    with pytest.raises(ValueError):
        _spec(planner_guidance="x" * 2001)
    (playbooks_dir() / "broken.toml").write_text("id = [", encoding="utf-8")
    (playbooks_dir() / "market-research.toml").write_text(
        'id = "market-research"\ntitle = "x"\nplanner_guidance = "x"\nsynth_guidance = "x"\n', encoding="utf-8"
    )
    found, errors = load_custom_playbooks()
    assert list(found) == ["partner-scan"]
    assert len(errors) == 2
    assert get_playbook("market-research").builtin is True


async def test_direct_run_skips_planner_and_synth(tmp_path):
    ws, fake, orch, _events = _setup(tmp_path)
    session = orch.direct("전자계약 시장 규모만 빠르게", None, 1.0)
    assert session.direct and len(session.plan.tasks) == 1
    task = session.plan.tasks[0]
    assert task.use_web and task.inputs == ["memo.md"] and task.effort == "medium"
    assert session.estimate["synthesizer"] == 0.0
    outcome = await orch.execute(session)
    roles = [c[0] for c in fake.calls]
    assert set(roles) == {"worker", "helper"} and roles.count("helper") == 2
    assert outcome.status == "completed"
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "t1 노트" in report and "### 출처" in report
    record = ws.load_run(session.run_id)
    assert record["mode"] == "direct" and record["summary"] == "- t1 요점"
    assert "Deliverable" not in fake.messages[("t1", "medium")]


async def test_direct_run_doc_playbook_has_no_web(tmp_path):
    _ws, _fake, orch, _events = _setup(tmp_path)
    session = orch.direct("회의록 요약", "doc-digest", 1.0)
    assert session.plan.tasks[0].use_web is False
    assert "Deliverable guidance" in orch._worker_message(session, *_states(session))


def _states(session):
    from claubiz.orchestrator import TaskState

    st = TaskState(task=session.plan.tasks[0])
    return st, {st.task.id: st}


async def test_follow_up_uses_base_run(tmp_path):
    ws, fake, orch, _events = _setup(tmp_path)
    first = await orch.plan("국내 전자계약 시장 조사", None)
    await orch.execute(first)
    second = await orch.plan("경쟁사 가격 비교 추가", None, base_run_id=first.run_id)
    planner_msg = fake.messages[("plan", "medium")]
    assert "국내 전자계약 시장 조사" in planner_msg and "earlier_findings" in planner_msg
    assert any(ref.startswith(first.run_id) for ref in second.context.reusable)
    assert "# 보고서" in second.base_report and "실행 r" not in second.base_report
    outcome = await orch.execute(second)
    synth_msg = fake.messages[("synth", "medium")]
    assert "<base_report" in synth_msg
    record = ws.load_run(second.run_id)
    assert record["base_run"] == first.run_id
    assert "made-up.example.com" in outcome.report_path.read_text(encoding="utf-8")

    direct = orch.direct("한 줄 요약", None, 1.0, base_run_id=first.run_id)
    assert "<base_report" in orch._worker_message(direct, *_states(direct))


async def test_follow_up_guards(tmp_path):
    ws, _fake, orch, _events = _setup(tmp_path)
    planned = await orch.plan("조사", None)
    with pytest.raises(RunError):
        await orch.plan("이어서", None, base_run_id=planned.run_id)
    secret_run = await orch.plan("기밀 포함", None, allow_confidential=True)
    await orch.execute(secret_run)
    assert ws.load_run(secret_run.run_id)["allow_confidential"] is True
    with pytest.raises(RunError):
        orch.direct("이어서", None, 1.0, base_run_id=secret_run.run_id)
    orch.direct("이어서", None, 1.0, allow_confidential=True, base_run_id=secret_run.run_id)


async def test_generate_brief_records_cost_and_audit():
    runtime = Runtime(Settings(), Ledger(), AuditLog(), EventBus(), None, FakeLLM())
    text, cost = await generate_brief(
        runtime, name="client-a", description="고객사 A 연락처 a@b.com", scope="research", web_note="none",
        hints="임원용",
    )
    assert text.startswith("## 목적") and cost == 0.004
    message = runtime.llm.messages[("brief", "low")]
    assert "a@b.com" not in message and "<operator_notes>" in message
    ok, _, _ = runtime.audit.verify()
    assert ok


def test_import_rejects_windows_reserved_names(tmp_path):
    ws = Workspace.create(WorkspaceConfig(name="names"))
    memo = tmp_path / "memo.md"
    memo.write_text("메모", encoding="utf-8")
    for bad in ("aux.md", "nul.txt", "report.md."):
        with pytest.raises(WorkspaceError, match="사용할 수 없는 파일 이름"):
            ws.import_file(memo, "internal", name=bad)
    assert ws.import_file(memo, "internal", name="auxiliary.md").name == "auxiliary.md"
