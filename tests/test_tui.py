import asyncio

from test_orchestrator import FakeLLM
from textual.widgets import Button, DataTable, Input, Markdown, Select, Static, TabbedContent, TextArea

from claubiz.audit import AuditLog
from claubiz.budget import Ledger
from claubiz.config import Settings
from claubiz.events import EventBus
from claubiz.runtime import Runtime
from claubiz.tui import ClaubizApp
from claubiz.tui.modals import CreateWorkspaceModal, RunViewerModal
from claubiz.tui.widgets import SafeMarkdownViewer, safe_web_url
from claubiz.workspace import Workspace, WorkspaceConfig

SIZE = (160, 46)


def fake_runtime():
    return Runtime(Settings(), Ledger(), AuditLog(), EventBus(), None, FakeLLM())


def make_app(initial=None):
    return ClaubizApp(initial_workspace=initial, runtime_factory=fake_runtime)


def test_safe_web_url():
    assert safe_web_url("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert safe_web_url(" http://x.example.org ") == "http://x.example.org"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "docs/report.md", "https://user:pw@x.com",
                "https://exa mple.com", "ftp://x.com", "https://", "x" * 3000):
        assert safe_web_url(bad) is None


async def test_command_palette_disabled():
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.ENABLE_COMMAND_PALETTE is False
        assert type(app.screen).__name__ != "CommandPalette"


async def test_create_workspace_modal():
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        assert app.ws is None
        await pilot.press("ctrl+n")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, CreateWorkspaceModal)
        modal.query_one("#f-name", Input).value = "Bad Name"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, CreateWorkspaceModal)
        assert str(modal.query_one(".modal-error", Static).render()).strip()
        modal.query_one("#f-name", Input).value = "client-a"
        modal.query_one("#f-desc", Input).value = "고객사 A"
        modal.query_one("#f-webmode", Select).value = "allow"
        await pilot.pause()
        modal.query_one("#f-domains", Input).value = "korea.kr, example.com"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, CreateWorkspaceModal)
        ws = Workspace.open("client-a")
        assert ws.config.web.allowed_domains == ("korea.kr", "example.com")
        assert app.ws is not None and app.ws.name == "client-a"


async def test_history_viewer_and_links(tmp_path):
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    memo = tmp_path / "memo.md"
    memo.write_text("메모", encoding="utf-8")
    ws.import_file(memo, "internal")
    app = make_app("acme")
    opened: list[str] = []
    async with app.run_test(size=SIZE) as pilot:
        app.open_url = lambda url, new_tab=True: opened.append(url)
        app.query_one("#goal", TextArea).text = "시장 조사"
        await pilot.press("f5")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#exec", Button).display
        await pilot.press("f6")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.query_one("#exec", Button).display

        await pilot.press("f3")
        await pilot.pause()
        runs = app.query_one("#runs", DataTable)
        assert runs.row_count == 1
        runs.focus()
        await pilot.press("enter")
        await pilot.pause(0.3)
        viewer = app.screen
        assert isinstance(viewer, RunViewerModal)
        assert viewer.report and "보고서" in viewer.report
        assert len(viewer.notes) == 4

        report_view = viewer.query_one("#report-view", SafeMarkdownViewer)
        before = report_view.navigator.location
        for href in ("https://stats.example.org/a", "file:///etc/passwd", "notes/t1.md"):
            report_view.document.post_message(Markdown.LinkClicked(report_view.document, href))
            await pilot.pause()
        assert opened == ["https://stats.example.org/a"]
        assert report_view.navigator.location == before

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RunViewerModal)
        await pilot.press("f7")
        await pilot.pause(0.3)
        assert isinstance(app.screen, RunViewerModal)


async def test_workspace_switch_refreshes_panels(tmp_path):
    Workspace.create(WorkspaceConfig(name="alpha", description="첫째"))
    beta = Workspace.create(WorkspaceConfig(name="beta", scope="inputs-only"))
    doc = tmp_path / "a.md"
    doc.write_text("문서", encoding="utf-8")
    beta.import_file(doc, "public")
    app = make_app("alpha")
    async with app.run_test(size=SIZE) as pilot:
        assert app.ws.name == "alpha"
        assert app.query_one("#inputs", DataTable).row_count == 0
        app.query_one("#ws-list").highlighted = 1
        await pilot.pause()
        assert app.ws.name == "beta"
        assert app.query_one("#inputs", DataTable).row_count == 1


async def _run_once(app, pilot, goal="시장 조사", key="f9"):
    app.query_one("#goal", TextArea).text = goal
    await pilot.press(key)
    await app.workers.wait_for_complete()
    await pilot.pause()
    if key == "f5":
        await pilot.press("f6")
        await app.workers.wait_for_complete()
        await pilot.pause()


async def test_direct_run_from_tui():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        await _run_once(app, pilot)
        record = next(iter(app.history.values()))
        assert record["mode"] == "direct" and record["status"] == "completed"
        assert {c[0] for c in app.runtime.llm.calls} == {"worker", "helper"}
        assert "바로" in str(app.query_one("#runs", DataTable).get_row_at(0)[2])


async def test_delete_run_with_confirm_card():
    from claubiz.tui.modals import ConfirmModal

    ws = Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        await _run_once(app, pilot)
        await _run_once(app, pilot, goal="두 번째")
        await pilot.press("f3")
        runs = app.query_one("#runs", DataTable)
        runs.focus()
        await pilot.pause()
        assert runs.row_count == 2
        target = app._cursor_run()
        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("n")
        await pilot.pause()
        assert runs.row_count == 2
        await pilot.press("backspace")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert runs.row_count == 2
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert runs.row_count == 1
        assert ws.load_run(target) is None
        assert app.runtime.ledger.spent_run(target) > 0


async def test_workspace_context_menu_delete():
    from claubiz.tui.modals import ConfirmModal, ContextMenu

    Workspace.create(WorkspaceConfig(name="alpha"))
    Workspace.create(WorkspaceConfig(name="beta"))
    app = make_app("alpha")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.click("#ws-list", offset=(4, 2), button=3)
        await pilot.pause()
        assert isinstance(app.screen, ContextMenu)
        assert app.ws.name == "beta"
        menu = app.screen.query_one("#context-menu")
        menu.highlighted = 4
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")
        await pilot.pause()
        assert Workspace.list_names() == ["alpha"]
        assert app.ws.name == "alpha"
        app.query_one("#ws-list").focus()
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert Workspace.list_names() == ["alpha"]


async def test_follow_up_with_ctrl_enter():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        await _run_once(app, pilot, key="f5")
        first = next(iter(app.history))
        await pilot.press("f3")
        app.query_one("#runs", DataTable).focus()
        await pilot.pause()
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert app.follow_run == first
        assert app.query_one("#follow-bar").display
        assert app.query_one("#tabs", TabbedContent).active == "tab-run"
        await _run_once(app, pilot, goal="가격 비교 추가")
        newest = next(iter(app.history.values()))
        assert newest["base_run"] == first
        assert app.follow_run is None and not app.query_one("#follow-bar").display


async def test_escape_cancels_and_hold_quits():
    Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    now = [100.0]
    async with app.run_test(size=SIZE) as pilot:
        app.clock = lambda: now[0]
        app.query_one("#goal", TextArea).text = "조사"
        await pilot.press("f5")
        await app.workers.wait_for_complete()
        await pilot.press("f6")
        await pilot.pause(0.02)
        assert app.running
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.running
        assert next(iter(app.history.values()))["status"] == "cancelled"
        exited = []
        app.exit = lambda *args, **kwargs: exited.append(True)
        now[0] += 5
        for _ in range(3):
            now[0] += 0.7
            await pilot.press("escape")
        for _ in range(12):
            now[0] += 0.3
            await pilot.press("escape")
        assert not exited
        now[0] += 5
        await pilot.press("escape")
        now[0] += 0.4
        for _ in range(10):
            await pilot.press("escape")
            now[0] += 0.09
        assert "계속 누르면 종료" in str(app.query_one("#hold-hint", Static).render())
        assert not exited
        for _ in range(10):
            await pilot.press("escape")
            now[0] += 0.09
        assert exited


async def test_import_picker_and_playbook_modal(tmp_path):
    from claubiz.tui.modals import ImportModal, PlaybookModal

    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "meeting-notes.md").write_text("회의" * 100, encoding="utf-8")
    (docs / "sub" / "plan.txt").write_text("계획", encoding="utf-8")
    (docs / "image.png").write_bytes(b"\x89PNG")
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(ImportModal(ws, base_dir=docs))
        await pilot.pause()
        await app.workers.wait_for_complete()
        modal = app.screen
        await pilot.press("@", "m", "e", "e", "t")
        await pilot.pause()
        assert [e.path.name for e in modal.matches] == ["meeting-notes.md"]
        await pilot.press("enter")
        await pilot.pause()
        modal.query_one("#f-path", Input).value = "@sub/"
        await pilot.pause()
        assert [e.path.name for e in modal.matches] == ["plan.txt"]
        await pilot.press("enter")
        await pilot.pause()
        assert [e.path.name for e in modal.selected] == ["meeting-notes.md", "plan.txt"]
        assert "합계" in str(modal.query_one("#f-total", Static).render())
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert {f.name for f in ws.inputs()} == {"meeting-notes.md", "plan.txt"}

        await pilot.click("#new-pb")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, PlaybookModal)
        modal.query_one("#p-id", Input).value = "partner-scan"
        modal.query_one("#p-title", Input).value = "협력사 발굴"
        modal.query_one("#p-plan", TextArea).text = "업종별로 나눔"
        modal.query_one("#p-synth", TextArea).text = "비교표"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.query_one("#pb", Select).value == "partner-scan"


async def test_create_workspace_with_ai_brief():
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        modal = app.screen
        modal.query_one("#f-name", Input).value = "client-b"
        modal.query_one("#f-brief-mode", Select).value = "ai"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "AI 초안" in str(modal.query_one(".modal-error", Static).render())
        modal.query_one("#gen").press()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert modal.query_one("#f-brief", TextArea).text.startswith("## 목적")
        await pilot.press("ctrl+s")
        await pilot.pause()
        ws = Workspace.open("client-b")
        assert "고객사 A" in ws.read_brief()


async def test_closing_modals_during_background_work(monkeypatch):
    import time as _time

    from claubiz.tui import modals

    original = modals.scan_files

    def slow_scan(*args, **kwargs):
        _time.sleep(0.3)
        return original(*args, **kwargs)

    monkeypatch.setattr(modals, "scan_files", slow_scan)
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    app = make_app("acme")
    async with app.run_test(size=SIZE) as pilot:
        app.push_screen(modals.ImportModal(ws))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause(0.4)
        assert app.is_running and not isinstance(app.screen, modals.ImportModal)

        started = asyncio.Event()

        async def slow_generator(**kwargs):
            started.set()
            await asyncio.sleep(5)
            return "x", 0.0

        app._generate_brief = slow_generator
        await pilot.press("ctrl+n")
        await pilot.pause()
        modal = app.screen
        modal.query_one("#f-name", Input).value = "client-c"
        modal.query_one("#f-brief-mode", Select).value = "ai"
        await pilot.pause()
        modal.query_one("#gen").press()
        await asyncio.wait_for(started.wait(), 2)
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.is_running and not isinstance(app.screen, modals.CreateWorkspaceModal)
        assert "client-c" not in Workspace.list_names()
