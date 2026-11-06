import os
import plistlib
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import hostos
from .paths import ensure_private_dir, home, home_path, write_private_bytes

LABEL = "com.claubiz.digest"
TASK_NAME = "\\claubiz\\digest"
TASK_FILE = "digest-task.xml"
MIN_INTERVAL = 5
MAX_INTERVAL = 240
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>claubiz digest check</Description>
    <URI>{uri}</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{interval}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


class AgentError(Exception):
    pass


Runner = Callable[..., subprocess.CompletedProcess]


def _backend() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "win32":
        return "schtasks"
    raise AgentError("백그라운드 실행 등록은 macOS(launchd)와 Windows(작업 스케줄러)에서만 지원함. 다른 OS는 "
                     "cron이나 systemd timer로 `claubiz digest run-due`를 주기 실행하면 됨")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def task_xml_path() -> Path:
    return home_path() / "agent" / TASK_FILE


def registration_path() -> Path:
    return task_xml_path() if sys.platform == "win32" else plist_path()


def log_path() -> Path:
    return ensure_private_dir(home() / "logs") / "agent.log"


def _check_interval(interval_minutes: int) -> None:
    if not MIN_INTERVAL <= interval_minutes <= MAX_INTERVAL:
        raise AgentError(f"확인 간격은 {MIN_INTERVAL}~{MAX_INTERVAL}분이어야 함")


def _run_args(data_dir: str) -> list[str]:
    return ["-m", "claubiz", "--home", data_dir, "digest", "run-due"]


def render_plist(interval_minutes: int, python: str | None = None) -> bytes:
    _check_interval(interval_minutes)
    data_dir = str(home_path())
    log = str(log_path())
    payload = {
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, *_run_args(data_dir)],
        "StartInterval": interval_minutes * 60,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "Umask": 0o077,
        "WorkingDirectory": data_dir,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return plistlib.dumps(payload, sort_keys=True)


def windowless_python(python: str | None = None) -> str:
    base = Path(python or sys.executable)
    candidate = base.with_name("pythonw.exe")
    return str(candidate if candidate.is_file() else base)


def render_task_xml(interval_minutes: int, user: str, python: str | None = None,
                    now: datetime | None = None) -> bytes:
    _check_interval(interval_minutes)
    data_dir = str(home_path())
    start = (now or datetime.now()).replace(microsecond=0).isoformat()
    text = TASK_XML.format(
        uri=escape(TASK_NAME),
        interval=interval_minutes,
        start=escape(start),
        user=escape(user),
        command=escape(windowless_python(python)),
        arguments=escape(subprocess.list2cmdline(_run_args(data_dir))),
        workdir=escape(data_dir),
    )
    return text.encode("utf-16")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _check_target(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if hostos.stat_is_link(st) or not stat.S_ISREG(st.st_mode) or not hostos.owned_by_me(path, st):
        raise AgentError(f"등록 파일 자리에 일반 파일이 아닌 항목이 있음: {path}")


def _touch_log() -> Path:
    log = log_path()
    os.close(hostos.open_fd(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    hostos.restrict_file(log)
    return log


def _schtasks() -> str:
    from .hostos import windows

    return str(Path(windows.system_directory()) / "schtasks.exe")


def _detail(result: subprocess.CompletedProcess) -> str:
    raw = result.stderr or result.stdout or b""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return text.strip()[:200] or str(result.returncode)


def _install_launchd(interval_minutes: int, runner: Runner) -> Path:
    data = render_plist(interval_minutes)
    target = plist_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_target(target)
    _touch_log()
    runner(["/bin/launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False, capture_output=True, timeout=30)
    write_private_bytes(target, data)
    result = runner(["/bin/launchctl", "bootstrap", _domain(), str(target)], check=False, capture_output=True,
                    timeout=30)
    if result.returncode != 0:
        raise AgentError(f"launchctl 등록 실패: {_detail(result)}")
    return target


def _install_task(interval_minutes: int, runner: Runner) -> Path:
    from .hostos import windows

    data = render_task_xml(interval_minutes, windows.current_user_sid())
    target = ensure_private_dir(home() / "agent") / TASK_FILE
    _check_target(target)
    _touch_log()
    write_private_bytes(target, data)
    result = runner([_schtasks(), "/Create", "/TN", TASK_NAME, "/XML", str(target), "/F"], check=False,
                    capture_output=True, timeout=30)
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise AgentError(f"작업 스케줄러 등록 실패: {_detail(result)}")
    return target


def install(interval_minutes: int, runner: Runner = subprocess.run) -> Path:
    if _backend() == "schtasks":
        return _install_task(interval_minutes, runner)
    return _install_launchd(interval_minutes, runner)


def uninstall(runner: Runner = subprocess.run) -> bool:
    backend = _backend()
    target = registration_path()
    _check_target(target)
    if backend == "schtasks":
        result = runner([_schtasks(), "/Delete", "/TN", TASK_NAME, "/F"], check=False, capture_output=True,
                        timeout=30)
        deleted = result.returncode == 0
    else:
        runner(["/bin/launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False, capture_output=True,
               timeout=30)
        deleted = False
    if target.exists():
        target.unlink()
        return True
    return deleted


def status(runner: Runner = subprocess.run) -> tuple[bool, bool]:
    if sys.platform not in ("darwin", "win32"):
        return False, False
    installed = registration_path().is_file()
    if sys.platform == "win32":
        command = [_schtasks(), "/Query", "/TN", TASK_NAME]
    else:
        command = ["/bin/launchctl", "print", f"{_domain()}/{LABEL}"]
    result = runner(command, check=False, capture_output=True, timeout=30)
    return installed, result.returncode == 0
