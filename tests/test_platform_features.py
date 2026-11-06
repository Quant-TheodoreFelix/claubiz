import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest
from support import assert_private

from claubiz import agent, cli
from claubiz.hostos import windows

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
SID = "S-1-5-21-1000-2000-3000-1001"


@pytest.fixture
def fake_windows(monkeypatch):
    if sys.platform == "win32":
        return
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows, "current_user_sid", lambda: SID)
    monkeypatch.setattr(windows, "system_directory", lambda: str(Path("C:/Windows/System32")))


def test_task_xml_runs_as_current_user_without_console(tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")
    data = agent.render_task_xml(15, SID, python=str(scripts / "python.exe"), now=datetime(2026, 11, 6, 9, 0, 30))
    assert data.startswith(b"\xff\xfe")
    root = ElementTree.fromstring(data)
    assert root.find("t:RegistrationInfo/t:URI", NS).text == agent.TASK_NAME
    assert root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text == "PT15M"
    assert root.find("t:Triggers/t:TimeTrigger/t:StartBoundary", NS).text == "2026-11-06T09:00:30"
    principal = root.find("t:Principals/t:Principal", NS)
    assert principal.find("t:UserId", NS).text == SID
    assert principal.find("t:LogonType", NS).text == "InteractiveToken"
    assert principal.find("t:RunLevel", NS).text == "LeastPrivilege"
    assert root.find("t:Settings/t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    command = root.find("t:Actions/t:Exec/t:Command", NS).text
    arguments = root.find("t:Actions/t:Exec/t:Arguments", NS).text
    assert Path(command).name == "pythonw.exe"
    assert arguments.startswith("-m claubiz --home ") and arguments.endswith(" digest run-due")
    with pytest.raises(agent.AgentError):
        agent.render_task_xml(1, SID)


def test_task_scheduler_install_status_uninstall(fake_windows):
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    path = agent.install(30, runner=runner)
    assert path == agent.task_xml_path() == agent.registration_path()
    assert_private(path, 0o600)
    assert Path(calls[0][0]).name == "schtasks.exe"
    assert calls[0][1:] == ["/Create", "/TN", agent.TASK_NAME, "/XML", str(path), "/F"]
    assert agent.status(runner=runner) == (True, True)
    assert calls[-1][1:] == ["/Query", "/TN", agent.TASK_NAME]
    assert agent.uninstall(runner=runner) and not path.exists()
    assert calls[-1][1:] == ["/Delete", "/TN", agent.TASK_NAME, "/F"]

    def failing(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, b"", "액세스가 거부되었습니다".encode())

    with pytest.raises(agent.AgentError, match="작업 스케줄러 등록 실패"):
        agent.install(30, runner=failing)
    assert not agent.task_xml_path().exists()


def test_background_output_goes_to_agent_log(fake_windows, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert cli.main(["ws", "list"]) == 0
    stream = sys.stdout
    assert stream is sys.stderr
    stream.close()
    assert "이름" in agent.log_path().read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path separators")
def test_tui_paths_accept_backslashes_and_drives(tmp_path):
    from claubiz.tui.modals import folder_hint, list_directory, looks_like_path

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "memo.md").write_text("x", encoding="utf-8")
    found = list_directory(f"{tmp_path}\\sub\\me", tmp_path, tmp_path / "excluded")
    assert [entry.path.name for entry in found] == ["memo.md"]
    dirs = list_directory(f"{tmp_path}\\", tmp_path, tmp_path / "excluded")
    assert any(entry.is_dir and entry.label == "sub\\" for entry in dirs)
    assert looks_like_path("C:") and looks_like_path("sub\\x") and not looks_like_path("memo")
    assert folder_hint(tmp_path).endswith("\\")
    assert os.sep == "\\"
