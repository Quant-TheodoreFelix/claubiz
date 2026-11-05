import os
import plistlib
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .paths import ensure_private_dir, home, home_path, write_private_bytes

LABEL = "com.claubiz.digest"
MIN_INTERVAL = 5
MAX_INTERVAL = 240


class AgentError(Exception):
    pass


Runner = Callable[..., subprocess.CompletedProcess]


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise AgentError("백그라운드 실행 등록은 macOS(launchd)에서만 지원함. 다른 OS는 cron 등으로 "
                         "`claubiz digest run-due`를 주기 실행하면 됨")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path() -> Path:
    return ensure_private_dir(home() / "logs") / "agent.log"


def render_plist(interval_minutes: int, python: str | None = None) -> bytes:
    if not MIN_INTERVAL <= interval_minutes <= MAX_INTERVAL:
        raise AgentError(f"확인 간격은 {MIN_INTERVAL}~{MAX_INTERVAL}분이어야 함")
    data_dir = str(home_path())
    log = str(log_path())
    payload = {
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, "-m", "claubiz", "--home", data_dir, "digest", "run-due"],
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


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _check_target(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise AgentError(f"등록 파일 자리에 일반 파일이 아닌 항목이 있음: {path}")


def install(interval_minutes: int, runner: Runner = subprocess.run) -> Path:
    _require_macos()
    data = render_plist(interval_minutes)
    target = plist_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_target(target)
    log = log_path()
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    os.chmod(log, 0o600)
    runner(["/bin/launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False, capture_output=True, timeout=30)
    write_private_bytes(target, data)
    result = runner(["/bin/launchctl", "bootstrap", _domain(), str(target)], check=False, capture_output=True,
                    timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise AgentError(f"launchctl 등록 실패: {detail or result.returncode}")
    return target


def uninstall(runner: Runner = subprocess.run) -> bool:
    _require_macos()
    target = plist_path()
    _check_target(target)
    runner(["/bin/launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False, capture_output=True, timeout=30)
    if target.exists():
        target.unlink()
        return True
    return False


def status(runner: Runner = subprocess.run) -> tuple[bool, bool]:
    if sys.platform != "darwin":
        return False, False
    installed = plist_path().is_file()
    result = runner(["/bin/launchctl", "print", f"{_domain()}/{LABEL}"], check=False, capture_output=True,
                    timeout=30)
    return installed, result.returncode == 0
