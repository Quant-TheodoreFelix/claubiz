import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta

from test_tui import SIZE, make_app
from textual.widgets import DataTable, Input, Select, Static, Switch, TabbedContent, TextArea

from claubiz.actions import ActionStore
from claubiz.digest import DigestStore, make_spec
from claubiz.knowledge import KnowledgeStore
from claubiz.tui.modals import (
    ActionExportModal,
    ConfirmModal,
    CreateWorkspaceModal,
    DigestModal,
    ProfileModal,
)
from claubiz.workspace import Workspace, WorkspaceConfig


async def _until(pilot, condition, tries=300):
    for _ in range(tries):
        if condition():
            return
        await pilot.pause(0.02)
    raise AssertionError("condition not reached")


async def _direct(app, pilot, goal="전자계약 시장 규모"):
    app.query_one("#goal", TextArea).text = goal
    await pilot.press("f9")
    await app.workers.wait_for_complete()
    await pilot.pause()


def _audit(app) -> list[dict]:
    return [json.loads(line) for line in app.runtime.audit.path.read_text(encoding="utf-8").splitlines()]


async def test_create_workspace_with_business_profile():
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, CreateWorkspaceModal)
        modal.query_one("#f-name", Input).value = "client-a"
        modal.query_one("#f-type", Select).value = "saas"
        modal.query_one("#f-industry", Input).value = "B2B 전자계약"
        modal.query_one("#f-competitors", Input).value = "모두싸인, 글로싸인"
        modal.query_one("#f-focus", TextArea).text = "경쟁사 가격 변화\n규제 동향"
        modal.query_one("#f-actions", Switch).value = False
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, CreateWorkspaceModal)
        cfg = Workspace.open("client-a").config
        assert cfg.profile.business_type == "saas" and cfg.profile.industry == "B2B 전자계약"
        assert cfg.profile.focus == "경쟁사 가격 변화\n규제 동향" and cfg.knowledge and cfg.actions == ()
        card = str(app.query_one("#ws-card", Static).render())
        assert "SaaS·IT 서비스" in card and "꺼짐" in card

        await pilot.press("f4")
        await pilot.pause()
        await pilot.click("#edit-profile")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ProfileModal)
        assert modal.query_one("#f-industry", Input).value == "B2B 전자계약"
        modal.query_one("#f-audience", Input).value = "대표"
        modal.query_one("#f-actions", Switch).value = True
        await pilot.press("ctrl+s")
        await pilot.pause()
        cfg = Workspace.open("client-a").config
        assert cfg.profile.audience == "대표" and len(cfg.actions) == 4
        assert app.ws.config.profile.audience == "대표"
        events = [e["event"] for e in _audit(app)]
        assert "workspace.profile_set" in events


async def test_run_feeds_knowledge_and_approval_inbox(tmp_path, monkeypatch):
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    opened = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: opened.append(args))
    monkeypatch.setattr(sys, "platform", "darwin")
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        await _direct(app, pilot)
        facts = app.query_one("#facts", DataTable)
        assert facts.row_count == 1 and "stats.example.org" in str(app.query_one("#fact-detail", Static).render())
        tab = app.query_one("#tabs", TabbedContent).get_tab("tab-actions")
        assert str(tab.label) == "제안 1"

        await pilot.press("f10")
        await pilot.pause()
        assert app.screen.focused is app.query_one("#action-list")
        await pilot.press("o")
        await pilot.pause()
        assert opened == []
        await pilot.press("y")
        await pilot.pause()
        action = ActionStore(ws).list()[0]
        assert action.status == "done" and (ws.root / "outbox" / action.output).read_text(encoding="utf-8")
        assert str(tab.label) == "제안"
        await pilot.press("o")
        await pilot.pause()
        assert opened and opened[0][0] == "/usr/bin/open" and opened[0][1].endswith(action.output)

        out = tmp_path / "share"
        out.mkdir()
        await pilot.press("c")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ActionExportModal)
        modal.query_one("#x-path", Input).value = str(out)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert [p.name for p in out.iterdir()] == [action.output]

        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")
        await pilot.pause()
        assert ActionStore(ws).list() == [] and not (ws.root / "outbox" / action.output).exists()
        events = [e["event"] for e in _audit(app)]
        for name in ("action.proposed", "action.approved", "action.opened", "action.exported", "action.deleted"):
            assert name in events

        await pilot.press("f4")
        facts.focus()
        await pilot.pause()
        await pilot.press("backspace")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert KnowledgeStore(ws).facts() == [] and facts.row_count == 0


async def test_history_proposals_and_delete_cascade():
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        await _direct(app, pilot)
        run_id = app.jobs["acme"].run_id
        await pilot.press("f3")
        await pilot.pause()
        await pilot.press("a")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert len(ActionStore(ws).list()) == 2
        await pilot.press("delete")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ConfirmModal) and "지식 1개와 행동 제안 2건" in modal.detail
        await pilot.press("y")
        await pilot.pause()
        assert ws.load_run(run_id) is None
        assert KnowledgeStore(ws).facts() == [] and ActionStore(ws).list() == []
        deleted = [e["data"] for e in _audit(app) if e["event"] == "run.deleted"]
        assert deleted == [{"workspace": "acme", "run_id": run_id, "knowledge": 1, "actions": 2, "via": "tui"}]


async def test_digest_runs_in_background_without_touching_composer():
    Workspace.create(WorkspaceConfig(name="acme"))
    Workspace.create(WorkspaceConfig(name="beta"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("f1")
        await pilot.pause()
        assert app.screen.focused is app.query_one("#digests")
        await pilot.click("#new-digest")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, DigestModal)
        modal.query_one("#d-title", Input).value = "경쟁사 소식"
        modal.query_one("#d-request", TextArea).text = "경쟁사 신제품과 가격"
        modal.query_one("#d-schedule", Input).value = "언젠가"
        await pilot.pause()
        assert "시각 형식" in str(modal.query_one("#d-next", Static).render())
        modal.query_one("#d-schedule", Input).value = "평일 오전 9시"
        modal.query_one("#d-cap", Input).value = "1"
        await pilot.pause()
        assert "다음 실행" in str(modal.query_one("#d-next", Static).render())
        await pilot.press("ctrl+s")
        await pilot.pause()
        spec = DigestStore(Workspace.open("acme")).load()[0]
        assert spec.schedule == "평일 09:00" and spec.model == "claude-sonnet-5-5"

        fake = app.runtime.llm
        fake.hold = asyncio.Event()
        fake.held = {"t1"}
        app.query_one("#digests", DataTable).focus()
        await pilot.press("r")
        await _until(pilot, lambda: ("start", "t1") in fake.order)
        await pilot.pause(1.1)
        assert "실행 중" in str(app.query_one("#digests", DataTable).get_row(spec.id)[3])
        assert app._job() is None and str(app.query_one("#run-state", Static).render()) == ""
        await pilot.press("r")
        await pilot.pause()
        assert len(app.digest_jobs) == 1
        app.query_one("#ws-list").highlighted = 1
        await pilot.pause()
        card = app.query_one("#jobs-card", Static)
        assert card.display and "정기 실행 중" in str(card.render())
        fake.hold.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not card.display
        app.query_one("#ws-list").highlighted = 0
        await pilot.pause()
        done = DigestStore(Workspace.open("acme")).get(spec.id)
        assert done.last_status == "completed" and done.seen_urls
        row = app.query_one("#runs", DataTable).get_row(done.last_run_id)
        assert "정기 · 경쟁사 소식" in str(row[2])

        app.query_one("#digests", DataTable).focus()
        await pilot.press("space")
        await pilot.pause()
        assert not DigestStore(Workspace.open("acme")).get(spec.id).enabled
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert DigestStore(Workspace.open("acme")).load() == []


async def test_scheduler_launches_due_digests_once():
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    store = DigestStore(ws)
    created = datetime.now() - timedelta(days=2)
    spec = store.add(make_spec(ws, title="규제", request="규제 변화", schedule="1시간마다", run_cap=1.0,
                               now=created))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        fake = app.runtime.llm
        fake.hold = asyncio.Event()
        fake.held = {"t1"}
        app._check_digests()
        app._check_digests()
        await _until(pilot, lambda: ("start", "t1") in fake.order)
        assert len(app.digest_jobs) == 1 and store.is_running(spec.id)
        fake.hold.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert fake.order.count(("start", "t1")) == 1
        finished = store.get(spec.id)
        assert finished.last_status == "completed" and not store.is_running(spec.id)
        app._check_digests()
        await pilot.pause()
        assert fake.order.count(("start", "t1")) == 1
        events = [e["event"] for e in _audit(app)]
        assert events.count("digest.started") == 1 and events.count("digest.finished") == 1
