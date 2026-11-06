import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "install.sh.in"
PS_TEMPLATE = ROOT / "scripts" / "install.ps1.in"
LOCK = ROOT / "requirements.lock"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
URL_CHARS = re.compile(r"[A-Za-z0-9._~:/%-]+")
WHEEL_NAME = re.compile(r"claubiz-(\d+\.\d+\.\d+)-py3-none-any\.whl")
PLACEHOLDER = re.compile(r"@[A-Z0-9_]+@")
REQUIRED_ENTRIES = (
    "claubiz/__init__.py",
    "claubiz/cli.py",
    "claubiz/tui/app.py",
    "claubiz/tui/app.tcss",
    "claubiz/locales/en.json",
)
FORBIDDEN_PARTS = ("__pycache__", ".DS_Store", ".pyc")
TEXT_PATHS = (
    "/install.sh",
    "/install.ps1",
    "/SHA256SUMS",
    "/releases/:release/install.sh",
    "/releases/:release/install.ps1",
    "/releases/:release/requirements.lock",
)
SITE_HEADERS = (
    "X-Content-Type-Options: nosniff",
    "Referrer-Policy: no-referrer",
    "Content-Security-Policy: default-src 'none'; frame-ancestors 'none'",
)


class ReleaseError(Exception):
    pass


def check_base_url(raw: str, allow_local_http: bool = False) -> str:
    value = raw.strip()
    if not URL_CHARS.fullmatch(value):
        raise ReleaseError(f"주소에 허용되지 않는 문자가 있음: {raw!r}")
    parts = urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise ReleaseError(f"주소 형식이 올바르지 않음: {raw}")
    if parts.scheme == "https":
        return value.rstrip("/")
    if parts.scheme == "http" and allow_local_http and parts.hostname in LOCAL_HOSTS:
        return value.rstrip("/")
    raise ReleaseError("배포 주소는 https여야 함 (로컬 시험은 --allow-local-http와 127.0.0.1)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lock_body(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith("#    uv export")]


def check_lock() -> None:
    result = subprocess.run(
        ["uv", "export", "--offline", "--no-dev", "--format", "requirements-txt", "--no-emit-project", "-q"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise ReleaseError(f"uv export 실패: {result.stderr.strip()[:300]}")
    if lock_body(result.stdout) != lock_body(LOCK.read_text(encoding="utf-8")):
        raise ReleaseError("requirements.lock이 uv.lock과 다름, 다시 생성 후 실행: "
                           "uv export --offline --no-dev --format requirements-txt --no-emit-project "
                           "-o requirements.lock")


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = data["project"]["version"]
    init = (ROOT / "src" / "claubiz" / "__init__.py").read_text(encoding="utf-8")
    if f'__version__ = "{version}"' not in init:
        raise ReleaseError(f"pyproject.toml 버전({version})과 claubiz.__version__이 다름")
    return version


def check_wheel(path: Path, version: str) -> None:
    match = WHEEL_NAME.fullmatch(path.name)
    if not match or match.group(1) != version:
        raise ReleaseError(f"예상과 다른 wheel 이름: {path.name}")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    missing = [entry for entry in REQUIRED_ENTRIES if entry not in names]
    if missing:
        raise ReleaseError(f"wheel에 필요한 파일이 없음: {', '.join(missing)}")
    unwanted = [name for name in names if any(part in name for part in FORBIDDEN_PARTS)]
    if unwanted:
        raise ReleaseError(f"wheel에 불필요한 파일이 있음: {', '.join(unwanted[:5])}")


def build_wheel(work: Path, offline: bool) -> Path:
    command = ["uv", "build", "--wheel", "--out-dir", str(work)]
    if offline:
        command.append("--offline")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        hint = "" if not offline else " (빌드 도구가 캐시에 없으면 --online으로 다시 실행)"
        raise ReleaseError(f"wheel 빌드 실패{hint}: {result.stderr.strip()[-300:]}")
    wheels = sorted(work.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseError(f"wheel이 하나가 아님: {[w.name for w in wheels]}")
    return wheels[0]


def render_installer(template: str, values: dict[str, str]) -> str:
    text = template
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    leftover = sorted(set(PLACEHOLDER.findall(text)))
    if leftover:
        raise ReleaseError(f"채워지지 않은 값: {', '.join(leftover)}")
    return text


def place_immutable(source: Path, target: Path) -> None:
    if target.exists():
        if sha256(target) != sha256(source):
            raise ReleaseError(f"같은 배포 ID에 다른 파일이 이미 있음: {target}")
        return
    shutil.copyfile(source, target)


def pages_headers() -> str:
    lines = ["/*", *(f"  {header}" for header in SITE_HEADERS)]
    for path in TEXT_PATHS:
        lines += [path, "  Content-Type: text/plain; charset=utf-8"]
    return "\n".join(lines) + "\n"


def write_text(path: Path, text: str, mode: int) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(mode)


def build_release(base_url: str, out: Path, offline: bool = True) -> dict[str, str]:
    check_lock()
    version = project_version()
    with tempfile.TemporaryDirectory(prefix="claubiz-release-") as tmp:
        wheel = build_wheel(Path(tmp), offline)
        check_wheel(wheel, version)
        wheel_sha = sha256(wheel)
        lock_sha = sha256(LOCK)
        release = f"{version}-{wheel_sha[:12]}"
        release_dir = out / "releases" / release
        release_dir.mkdir(parents=True, exist_ok=True)
        place_immutable(wheel, release_dir / wheel.name)
        place_immutable(LOCK, release_dir / "requirements.lock")
        wheel_name = wheel.name
    values = {
        "VERSION": version,
        "RELEASE": release,
        "BASE_URL": base_url,
        "WHEEL": wheel_name,
        "WHEEL_SHA256": wheel_sha,
        "LOCK_SHA256": lock_sha,
    }
    for template, name in ((TEMPLATE, "install.sh"), (PS_TEMPLATE, "install.ps1")):
        installer = render_installer(template.read_text(encoding="utf-8"), values)
        write_text(release_dir / name, installer, 0o644)
        write_text(out / name, installer, 0o644)
    write_text(out / "_headers", pages_headers(), 0o644)
    for path in (release_dir / wheel_name, release_dir / "requirements.lock"):
        path.chmod(0o644)
    listed = [out / "install.sh", out / "install.ps1", release_dir / "install.sh", release_dir / "install.ps1",
              release_dir / wheel_name, release_dir / "requirements.lock"]
    sums = "".join(f"{sha256(path)}  {path.relative_to(out).as_posix()}\n" for path in listed)
    write_text(out / "SHA256SUMS", sums, 0o644)
    return {"version": version, "release": release, "installer_sha256": sha256(out / "install.sh"),
            "ps_installer_sha256": sha256(out / "install.ps1"), "base_url": base_url, "out": str(out)}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if (stream.encoding or "").lower().replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="claubiz 설치 스크립트와 배포 묶음 생성")
    parser.add_argument("--base-url", required=True, help="배포 묶음을 올릴 https 주소 (예: https://get.example.com)")
    parser.add_argument("--out", default=str(ROOT / "dist" / "site"), help="출력 폴더 (기본 dist/site)")
    parser.add_argument("--allow-local-http", action="store_true", help="http://127.0.0.1 시험용 주소 허용")
    parser.add_argument("--online", action="store_true", help="빌드 도구를 내려받을 수 있게 허용")
    args = parser.parse_args(argv)
    try:
        base = check_base_url(args.base_url, args.allow_local_http)
        out = Path(args.out).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        info = build_release(base, out, offline=not args.online)
    except ReleaseError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(f"배포 ID       {info['release']}")
    print(f"출력 폴더     {info['out']}")
    print(f"install.sh    sha256 {info['installer_sha256']}")
    print(f"install.ps1   sha256 {info['ps_installer_sha256']}")
    print()
    print("설치 명령 (macOS, Linux)")
    print(f"  curl -fsSL {base}/install.sh | sh")
    print("설치 명령 (Windows PowerShell)")
    print(f"  irm {base}/install.ps1 | iex")
    print()
    print("검증 후 설치 (macOS, Linux)")
    print(f"  curl -fsSLO {base}/install.sh")
    print(f"  echo '{info['installer_sha256']}  install.sh' | shasum -a 256 -c - && sh install.sh")
    print("검증 후 설치 (Windows PowerShell)")
    print(f"  irm {base}/install.ps1 -OutFile install.ps1")
    print(f"  if ((Get-FileHash install.ps1).Hash -eq '{info['ps_installer_sha256']}') "
          "{ & ([scriptblock]::Create((Get-Content -Raw -Encoding UTF8 .\\install.ps1))) }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
