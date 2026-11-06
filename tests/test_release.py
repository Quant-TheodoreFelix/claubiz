import hashlib
import importlib.util
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("claubiz_release", ROOT / "scripts" / "release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def fake_wheel(folder: Path, extra: str = "", version: str = "0.1.0") -> Path:
    path = folder / f"claubiz-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for entry in release.REQUIRED_ENTRIES:
            archive.writestr(entry, f"# {entry}{extra}\n")
    return path


def test_base_url_rules():
    assert release.check_base_url("https://get.example.com/") == "https://get.example.com"
    assert release.check_base_url("https://example.com/claubiz") == "https://example.com/claubiz"
    assert release.check_base_url("http://127.0.0.1:8765", allow_local_http=True) == "http://127.0.0.1:8765"
    for bad in ("http://example.com", "http://127.0.0.1:8765", "ftp://example.com", "https://user:pw@example.com",
                "https://example.com/?x=1", "https://example.com/#a", "https://exa mple.com",
                "https://example.com/'$(id)'", "https://"):
        with pytest.raises(release.ReleaseError):
            release.check_base_url(bad)
    with pytest.raises(release.ReleaseError):
        release.check_base_url("http://example.com", allow_local_http=True)


def test_render_fills_every_value():
    template = (ROOT / "scripts" / "install.sh.in").read_text(encoding="utf-8")
    values = {"VERSION": "0.1.0", "RELEASE": "0.1.0-abcdef123456", "BASE_URL": "https://get.example.com",
              "WHEEL": "claubiz-0.1.0-py3-none-any.whl", "WHEEL_SHA256": "a" * 64, "LOCK_SHA256": "b" * 64}
    text = release.render_installer(template, values)
    assert "@" not in "".join(line for line in text.splitlines() if line.startswith("CLAUBIZ_"))
    with pytest.raises(release.ReleaseError, match="LOCK_SHA256"):
        release.render_installer(template, {k: v for k, v in values.items() if k != "LOCK_SHA256"})
    sh = shutil.which("sh")
    if sh:
        result = subprocess.run([sh, "-n"], input=text, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr


def test_wheel_content_checks(tmp_path):
    good = fake_wheel(tmp_path)
    release.check_wheel(good, "0.1.0")
    with pytest.raises(release.ReleaseError, match="이름"):
        release.check_wheel(good, "0.2.0")
    with zipfile.ZipFile(good, "a") as archive:
        archive.writestr("claubiz/__pycache__/cli.cpython-312.pyc", b"")
    with pytest.raises(release.ReleaseError, match="불필요"):
        release.check_wheel(good, "0.1.0")


def test_build_release_layout_and_immutability(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    version = release.project_version()
    source = fake_wheel(work, version=version)

    def build(folder: Path, offline: bool) -> Path:
        return Path(shutil.copy(source, folder))

    monkeypatch.setattr(release, "check_lock", lambda: None)
    monkeypatch.setattr(release, "build_wheel", build)
    out = tmp_path / "site"
    info = release.build_release("https://get.example.com", out)
    rel = out / "releases" / info["release"]
    wheel_sha = hashlib.sha256((rel / f"claubiz-{version}-py3-none-any.whl").read_bytes()).hexdigest()
    assert info["release"] == f"{version}-{wheel_sha[:12]}"
    installer = (out / "install.sh").read_text(encoding="utf-8")
    assert installer == (rel / "install.sh").read_text(encoding="utf-8")
    assert f"CLAUBIZ_WHEEL_SHA256='{wheel_sha}'" in installer
    assert "CLAUBIZ_SOURCE='https://get.example.com'" in installer
    headers = (out / "_headers").read_text(encoding="utf-8").splitlines()
    for path in ("/install.sh", "/SHA256SUMS", "/releases/:release/install.sh"):
        assert headers[headers.index(path) + 1] == "  Content-Type: text/plain; charset=utf-8"
    assert "  X-Content-Type-Options: nosniff" in headers
    assert not any("whl" in line for line in headers)
    sums = dict(line.split("  ")[::-1] for line in (out / "SHA256SUMS").read_text().splitlines())
    for name, digest in sums.items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert release.build_release("https://get.example.com", out)["release"] == info["release"]
    lock = rel / "requirements.lock"
    lock.chmod(0o600)
    lock.write_text("tampered\n")
    with pytest.raises(release.ReleaseError, match="같은 배포 ID"):
        release.build_release("https://get.example.com", out)
