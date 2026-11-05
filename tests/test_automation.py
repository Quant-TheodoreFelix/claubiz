import asyncio
import csv
import email
import io
import json
import os
from datetime import date, datetime, timedelta
from email.policy import default as default_policy

import pytest
from test_orchestrator import PROPOSED, FakeLLM, _setup

from claubiz.actions import ActionError, ActionStore, parse_proposals, render_event
from claubiz.audit import AuditLog
from claubiz.budget import Ledger
from claubiz.config import Settings
from claubiz.derived import derived_counts, purge_derived
from claubiz.digest import (
    MAX_FAILURES,
    DigestError,
    DigestStore,
    make_spec,
    run_due,
)
from claubiz.events import EventBus
from claubiz.knowledge import Fact, KnowledgeStore, select_facts
from claubiz.orchestrator import Orchestrator
from claubiz.planner import build_context, planner_message
from claubiz.runtime import Runtime
from claubiz.schedule import ScheduleError, parse_schedule
from claubiz.workspace import BusinessProfile, Workspace, WorkspaceConfig


def test_profile_round_trip_and_system_prompt(tmp_path):
    profile = BusinessProfile(business_type="saas", industry="B2B 전자계약", customers="중견 제조사 담당자 010-1234-5678",
                              focus="경쟁사 가격 변화\n규제 동향")
    ws = Workspace.create(WorkspaceConfig(name="acme", profile=profile, actions=("doc", "email")))
    again = Workspace.open("acme")
    assert again.config.profile == profile and again.config.actions == ("doc", "email") and again.config.knowledge
    orch = Orchestrator(Settings(), ws, FakeLLM(), Ledger(), AuditLog(), EventBus())
    system = "\n".join(b["text"] for b in orch._system("P"))
    assert "<business_profile>" in system and "SaaS·IT 서비스" in system and "B2B 전자계약" in system
    assert "010-1234-5678" not in system and "[전화번호 가림]" in system
    assert orch._system("P")[-1]["cache_control"] == {"type": "ephemeral"}
    with pytest.raises(ValueError):
        BusinessProfile(business_type="casino")
    with pytest.raises(ValueError):
        WorkspaceConfig(name="x", actions=("shell",))


async def test_knowledge_is_learned_then_reused(tmp_path):
    ws, fake, orch, events = _setup(tmp_path)
    session = await orch.plan("국내 전자계약 시장 조사", "market-research")
    outcome = await orch.execute(session)
    assert outcome.status == "completed" and outcome.learned == 1
    facts = KnowledgeStore(ws).facts()
    assert [f.sources for f in facts] == [["https://stats.example.org/a"]]
    assert facts[0].classification == "internal" and facts[0].run_id == session.run_id
    assert "made-up" not in json.dumps([f.model_dump() for f in facts])
    assert "<report>" in fake.helper_messages["learn"] and "010-1234-5678" not in fake.helper_messages["learn"]
    record = ws.load_run(session.run_id)
    assert record["knowledge"] == 1 and len(record["actions"]) == 1
    assert any("메일 초안 제안 제외" in w for w in record["warnings"])
    assert any(e.kind == "knowledge.updated" for e in events)

    ctx = build_context(ws, None, False)
    message = planner_message("전자계약 시장 규모 업데이트", ws, None, ctx, orch.settings)
    assert "<known_facts>" in message and facts[0].id in message
    second = await orch.plan("전자계약 시장 규모 업데이트", None)
    await orch.execute(second)
    assert facts[0].id in fake.messages[("synth", second.plan.synth_effort)]
    assert facts[0].id in second.facts_used
    report = ws.read_report(second.run_id)
    assert "검증되지 않은 링크" in report and "stats.example.org" not in report.split("검증되지 않은 링크")[1]


async def test_confidential_facts_stay_behind_the_opt_in(tmp_path):
    ws, _fake, orch, _events = _setup(tmp_path)
    secret = await orch.plan("기밀 조사", None, allow_confidential=True)
    secret.plan.tasks[2].inputs = ["secret.md"]
    await orch.execute(secret)
    facts = KnowledgeStore(ws).facts()
    assert facts and all(f.classification == "confidential" for f in facts)
    assert build_context(ws, None, False).facts == ()
    assert len(build_context(ws, None, True).facts) == len(facts)
    off = ws.config.model_copy(update={"knowledge": False})
    ws.save_config(off)
    assert build_context(ws, None, True).facts == ()


def test_select_facts_prefers_overlap_and_recency():
    today = date(2026, 11, 5)

    def fact(fid, text, as_of, cls="internal"):
        return Fact(id=fid, text=text, topic="", sources=["https://a.example.com"], as_of=as_of, added="x",
                    run_id="r20261105-000000-aaaaaa", classification=cls)

    facts = [fact("k0000000001", "전자계약 시장 규모 1조 원", "2026-10-01"),
             fact("k0000000002", "물류 로봇 수요 증가", "2026-10-02"),
             fact("k0000000003", "전자계약 규제 개정 시행", "2025-01-01"),
             fact("k0000000004", "전자계약 기밀 단가", "2026-11-01", "confidential")]
    chosen = select_facts(facts, "전자계약 시장 조사", 5, lambda c: c != "confidential", today=today)
    assert [f.id for f in chosen] == ["k0000000001", "k0000000003"]
    assert select_facts(facts, "", 5) == [] and select_facts(facts, "전자계약", 0) == []


def test_schedule_parsing():
    assert parse_schedule("매일 09:00").describe() == "매일 09:00"
    assert parse_schedule("평일 오전 8시 30분").describe() == "평일 08:30"
    assert parse_schedule("월,목 10:00").describe() == "매주 월,목 10:00"
    assert parse_schedule("월수금 오후 3시반, 18:00").describe() == "매주 월,수,금 15:30, 18:00"
    assert parse_schedule("주말 9시").describe() == "주말 09:00"
    assert parse_schedule("6시간마다").describe() == "6시간마다"
    assert parse_schedule("07:15").describe() == "매일 07:15"
    for bad in ("", "매일", "매일 25:00", "화성 09:00", "0시간마다", "오후 13시", "1000시간마다"):
        with pytest.raises(ScheduleError):
            parse_schedule(bad)
    weekdays = parse_schedule("평일 09:00")
    friday_evening = datetime(2026, 11, 6, 18, 0)
    assert weekdays.next_after(friday_evening) == datetime(2026, 11, 9, 9, 0)
    assert weekdays.next_after(datetime(2026, 11, 9, 8, 59)) == datetime(2026, 11, 9, 9, 0)
    assert parse_schedule("6시간마다").next_after(datetime(2026, 11, 5, 10, 7, 30)) == datetime(2026, 11, 5, 16, 7)


def test_digest_store_claims_once_and_disables_after_failures():
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    store = DigestStore(ws)
    created = datetime(2026, 11, 5, 8, 0)
    spec = store.add(make_spec(ws, title="규제 동향", request="개인정보 규제 변화", schedule="매일 09:00", now=created))
    assert spec.model == "claude-sonnet-5-5" and spec.next_due() == datetime(2026, 11, 5, 9, 0)
    assert store.due(datetime(2026, 11, 5, 8, 59)) == []
    late = datetime(2026, 11, 7, 12, 0)
    assert [s.id for s in store.due(late)] == [spec.id]
    claim = store.claim(spec.id, late)
    assert claim is not None and store.claim(spec.id, late, force=True) is None
    assert store.get(spec.id).last_status == "running" and store.due(late) == []
    store.finish(claim, "r20261107-120000-abcdef", "completed", "# 정리\n- [a](https://news.example.com/1)\n")
    claim.release()
    done = store.get(spec.id)
    assert done.last_success_run == "r20261107-120000-abcdef" and done.seen_urls == ["https://news.example.com/1"]
    assert done.next_due() == datetime(2026, 11, 8, 9, 0)
    assert store.claim(spec.id, datetime(2026, 11, 7, 12, 30)) is None
    moment = datetime(2026, 11, 8, 9, 0)
    for _ in range(MAX_FAILURES + 1):
        moment += timedelta(hours=1)
        claim = store.claim(spec.id, moment, force=True)
        store.finish(claim, "r20261108-090000-abcdef", "cancelled")
        claim.release()
    assert store.get(spec.id).enabled and store.get(spec.id).failures == 0
    for i in range(MAX_FAILURES):
        moment += timedelta(days=1)
        claim = store.claim(spec.id, moment, force=True)
        store.finish(claim, f"r2026111{i}-090000-abcdef", "failed")
        claim.release()
    assert not store.get(spec.id).enabled and store.get(spec.id).failures == MAX_FAILURES
    with pytest.raises(DigestError):
        make_spec(ws, title="x", request="y", schedule="매일 9시", max_searches=99)
    with pytest.raises(DigestError):
        make_spec(ws, title="x", request="y", schedule="언젠가")
    only_inputs = Workspace.create(WorkspaceConfig(name="docs", scope="inputs-only"))
    with pytest.raises(DigestError):
        DigestStore(only_inputs).add(make_spec(only_inputs, title="x", request="y", schedule="매일 9시"))
    assert store.remove(spec.id) and store.get(spec.id) is None


async def test_digest_run_reports_only_new_items(tmp_path):
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    memo = tmp_path / "memo.md"
    memo.write_text("내부 메모", encoding="utf-8")
    ws.import_file(memo, "internal")
    fake = FakeLLM()
    runtime = Runtime(Settings(), Ledger(), AuditLog(), EventBus(), None, fake)
    store = DigestStore(ws)
    spec = store.add(make_spec(ws, title="경쟁사 소식", request="경쟁사 신제품과 가격 변화",
                               schedule="매일 09:00", run_cap=1.0, now=datetime(2026, 11, 1, 8, 0)))
    store.update(spec.id, seen_urls=["https://old.example.com/a"])
    results = await run_due(runtime, datetime(2026, 11, 5, 10, 0))
    assert results == [("acme", spec.id, "completed")]
    worker = next(c for c in fake.calls if c[0] == "worker")
    assert worker[2] == "low" and worker[3] is True
    message = fake.messages[("t1", "low")]
    assert "Reporting period: from 2026-10-29 10:00" in message and "https://old.example.com/a" in message
    assert "memo.md" not in message and "<documents>" not in message
    assert fake.models[-3][2] == "claude-sonnet-5-5"
    done = store.get(spec.id)
    record = ws.load_run(done.last_run_id)
    assert record["digest"] == {"id": spec.id, "title": "경쟁사 소식"} and record["status"] == "completed"
    assert record["plan"]["tasks"][0]["max_searches"] == 4
    assert done.last_success == "2026-11-05T10:00:00" and "https://stats.example.org/a" in done.seen_urls
    assert await run_due(runtime, datetime(2026, 11, 5, 11, 0)) == []
    second = await run_due(runtime, datetime(2026, 11, 6, 9, 1))
    assert second == [("acme", spec.id, "completed")]
    message = [m for (tid, _), m in fake.messages.items() if tid == "t1"][-1]
    assert "Reporting period: from 2026-11-05 10:00" in message and "<previous_digest>" in message
    lines = [json.loads(line) for line in runtime.audit.path.read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in lines].count("digest.finished") == 2


def test_proposals_are_validated(tmp_path):
    raw = json.dumps({"actions": PROPOSED["actions"] + [
        {"type": "event", "title": "발표회", "reason": "", "to": [], "cc": [], "subject": "", "body": "",
         "start": "2026-13-01", "end": "", "location": "", "columns": [], "rows": []},
        {"type": "shell", "title": "rm", "reason": "", "to": [], "cc": [], "subject": "", "body": "rm -rf /",
         "start": "", "end": "", "location": "", "columns": [], "rows": []},
    ]})
    actions, problems = parse_proposals(raw, run_id="r20261105-000000-aaaaaa", allowed=("doc", "email", "event"),
                                        classification="internal")
    assert [a.type for a in actions] == ["doc"] and len(problems) == 3


def _action(kind: str, title: str, **fields):
    item = {"type": kind, "title": title, "reason": "근거", "to": [], "cc": [], "subject": "", "body": "",
            "start": "", "end": "", "location": "", "columns": [], "rows": []}
    item.update(fields)
    actions, problems = parse_proposals(json.dumps({"actions": [item]}), run_id="r20261105-000000-aaaaaa",
                                        allowed=("email", "event", "table", "doc"), classification="internal")
    assert not problems, problems
    return actions[0]


def test_approved_actions_write_safe_files(tmp_path):
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    store = ActionStore(ws)
    mail = _action("email", "고객 공유", to=["kim@example.com"], subject="시장 업데이트\r\nBcc: evil@example.com",
                   body="안녕하세요.\n요약 공유드립니다.")
    event = _action("event", "규제 시행일, 확인", start="2026-12-01T09:30", location="서울; 본사",
                    body="세부 내용\n" + "가" * 60)
    table = _action("table", "경쟁사 표", columns=["회사", "메모"],
                    rows=[["A", "=HYPERLINK(\"http://x\")"], ["B", "-12.5"], ["C"]])
    doc = _action("doc", "메모", body="- 할 일")
    for action in (mail, event, table, doc):
        store.save(action)
    assert store.pending() == 4

    done = store.approve(mail.id)
    path = store.output_path(done)
    assert path.parent == ws.root / "outbox" and path.suffix == ".eml" and path.stat().st_mode & 0o777 == 0o600
    raw = path.read_bytes()
    parsed = email.message_from_bytes(raw, policy=default_policy)
    assert parsed["X-Unsent"] == "1" and parsed["To"] == "kim@example.com" and parsed["Bcc"] is None
    assert parsed["Subject"] == "시장 업데이트 Bcc: evil@example.com"
    assert parsed.get_content_type() == "text/plain" and "요약 공유드립니다" in parsed.get_content()
    with pytest.raises(ActionError):
        store.approve(mail.id)

    ics = store.output_path(store.approve(event.id)).read_bytes().decode("utf-8")
    assert "SUMMARY:규제 시행일\\, 확인" in ics and "LOCATION:서울\\; 본사" in ics
    assert "DTSTART:20261201T093000" in ics and "DTEND:20261201T103000" in ics
    assert "ATTENDEE" not in ics and "VALARM" not in ics and "ORGANIZER" not in ics
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n"))

    data = store.output_path(store.approve(table.id)).read_bytes().decode("utf-8")
    assert data.startswith("﻿")
    rows = list(csv.reader(io.StringIO(data.lstrip("﻿"))))
    assert rows == [["회사", "메모"], ["A", "'=HYPERLINK(\"http://x\")"], ["B", "-12.5"], ["C", ""]]

    rejected = store.reject(doc.id)
    assert rejected.status == "rejected" and rejected.output == "" and store.pending() == 0

    out = tmp_path / "share"
    out.mkdir()
    copied, digest = store.export(mail.id, str(out))
    assert copied.read_bytes() == raw and len(digest) == 64
    with pytest.raises(ActionError):
        store.export(doc.id, str(out))
    with pytest.raises(ActionError):
        store.export(mail.id, str(ws.root))
    path.write_bytes(raw + b"tampered")
    with pytest.raises(ActionError):
        store.export(mail.id, str(out))
    assert store.remove(mail.id) and not path.exists()


def test_event_all_day_and_bounds():
    action = _action("event", "마감", start="2026-12-01", end="2026-12-02")
    ics = render_event(action.title, action.draft()).decode()
    assert "DTSTART;VALUE=DATE:20261201" in ics and "DTEND;VALUE=DATE:20261203" in ics


async def test_manual_proposals_and_delete_cascade(tmp_path):
    ws, _fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("조사", None)
    await orch.execute(session)
    assert derived_counts(ws, session.run_id) == (1, 1)
    proposed = await orch.propose_actions(session.run_id)
    assert len(proposed) == 1 and len(ws.load_run(session.run_id)["actions"]) == 2
    other = await orch.plan("미실행", None)
    with pytest.raises(Exception, match="완료된 실행"):
        await orch.propose_actions(other.run_id)
    assert purge_derived(ws, session.run_id) == (1, 2)
    assert KnowledgeStore(ws).facts() == [] and ActionStore(ws).list() == []


async def test_estimate_includes_helpers_and_resume_keeps_it(tmp_path):
    ws, fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("조사", None)
    estimate = session.estimate
    assert estimate["helper"] > 0
    assert estimate["total"] == pytest.approx(sum(estimate["tasks"].values()) + estimate["synthesizer"]
                                              + estimate["helper"])
    ws.save_config(ws.config.model_copy(update={"knowledge": False, "actions": ()}))
    again = await orch.plan("조사", None)
    assert again.estimate["helper"] == 0.0
    outcome = await orch.execute(again)
    assert outcome.learned == 0 and outcome.proposed == []
    assert not any(c[0] == "helper" for c in fake.calls[-3:])


async def test_concurrent_claims_across_handles():
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    spec = DigestStore(ws).add(make_spec(ws, title="t", request="r", schedule="1시간마다",
                                         now=datetime(2026, 11, 5, 8, 0)))
    moment = datetime(2026, 11, 5, 10, 0)
    claims = await asyncio.gather(*(asyncio.to_thread(DigestStore(Workspace.open("acme")).claim, spec.id, moment)
                                    for _ in range(4)))
    won = [c for c in claims if c is not None]
    assert len(won) == 1
    won[0].release()
    assert os.path.exists(ws.root / f"digest-{spec.id}.lock")


def test_launch_agent_plist_and_install_with_fake_runner(monkeypatch):
    import plistlib
    import subprocess
    import sys

    from claubiz import agent

    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    data = plistlib.loads(agent.render_plist(15, python="/opt/py/bin/python"))
    assert data["ProgramArguments"][:3] == ["/opt/py/bin/python", "-m", "claubiz"]
    assert data["ProgramArguments"][-2:] == ["digest", "run-due"] and "--home" in data["ProgramArguments"]
    assert data["StartInterval"] == 900 and data["Umask"] == 0o077 and data["RunAtLoad"] is False
    with pytest.raises(agent.AgentError):
        agent.render_plist(1)
    path = agent.install(30, runner=runner)
    assert path == agent.plist_path() and path.stat().st_mode & 0o777 == 0o600
    assert agent.log_path().stat().st_mode & 0o777 == 0o600
    assert calls[0][1] == "bootout" and calls[1][:2] == ["/bin/launchctl", "bootstrap"]
    assert agent.uninstall(runner=runner) and not path.exists()
    path.symlink_to("/etc/hosts")
    with pytest.raises(agent.AgentError):
        agent.install(30, runner=runner)
