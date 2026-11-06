import json
import os

import httpx2
import pytest
from support import assert_private

from claubiz.audit import AuditLog
from claubiz.config import ApiConfig, Settings
from claubiz.paths import UnsafePathError, confine, ensure_private_dir
from claubiz.policy import PolicyError, may_send, redact_pii, validate_domains
from claubiz.secrets import SecretError, SecretValue, redact, scrub_credential_env, validate_key
from claubiz.transport import EgressBlocked, EgressGuardTransport

VALID_KEY = "sk-ant-api03-" + "A" * 80


def test_validate_key_accepts_api_key_and_rejects_others():
    assert validate_key(VALID_KEY + "\n").reveal() == VALID_KEY
    with pytest.raises(SecretError):
        validate_key("not-a-key")
    with pytest.raises(SecretError):
        validate_key("sk-ant-admin01-" + "B" * 80)
    with pytest.raises(SecretError):
        validate_key("sk-ant-oat01-" + "C" * 80)


def test_secret_value_never_prints():
    value = SecretValue(VALID_KEY)
    assert VALID_KEY not in repr(value)
    assert VALID_KEY not in str(value)
    assert VALID_KEY not in f"{value}"
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(value)


def test_redact_masks_keys():
    assert "AAAA" not in redact(f"key={VALID_KEY}")


def test_scrub_credential_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    removed = scrub_credential_env()
    assert "ANTHROPIC_BASE_URL" in removed and "ANTHROPIC_BASE_URL" not in os.environ


def test_confine_rejects_traversal(tmp_path):
    root = ensure_private_dir(tmp_path / "ws")
    for bad in ("..", "../x", "a/b", "", ".", "x\x00y"):
        with pytest.raises(UnsafePathError):
            confine(root, bad)
    target = tmp_path / "outside.txt"
    target.write_text("secret")
    (root / "link").symlink_to(target)
    with pytest.raises(UnsafePathError):
        confine(root, "link")
    assert confine(root, "ok.md") == root.resolve() / "ok.md"


def test_confine_rejects_names_that_break_on_windows(tmp_path):
    root = ensure_private_dir(tmp_path / "ws")
    for bad in ("CON", "nul.txt", "Com1.md", "LPT\u00b9", "con .txt", "a.", "a ", "a:b", "x|y", "q?", "t\tab"):
        with pytest.raises(UnsafePathError):
            confine(root, bad)
    for good in ("console.txt", "conx", "report.md", "한글 자료 (1).txt", "digest-ab12.lock"):
        assert confine(root, good).name == good


def test_private_dir_permissions(tmp_path):
    path = tmp_path / "p"
    path.mkdir(mode=0o755)
    ensure_private_dir(path)
    assert_private(path, 0o700)


def test_domain_validation():
    assert validate_domains(["Example.COM", "example.com", " docs.anthropic.com "]) == ("example.com", "docs.anthropic.com")
    for bad in ("http://x.com", "10.0.0.1", "localhost", "intranet.local", "x.com/path", "user@x.com", "nodot"):
        with pytest.raises(PolicyError):
            validate_domains([bad])


def test_pii_redaction():
    text = "홍길동 900101-1234567, 010-1234-5678, 02-123-4567, hong@example.co.kr, 카드 4111 1111 1111 1111, 주문 1234567890123"
    out, counts = redact_pii(text)
    assert "900101-1234567" not in out
    assert "010-1234-5678" not in out
    assert "02-123-4567" not in out
    assert "hong@example.co.kr" not in out
    assert "4111 1111 1111 1111" not in out
    assert "1234567890123" in out
    assert counts["주민등록번호"] == 1 and counts["카드번호"] == 1 and counts["전화번호"] == 2


def test_classification_policy():
    assert may_send("public", "internal", False)
    assert may_send("internal", "internal", False)
    assert not may_send("internal", "public", False)
    assert not may_send("confidential", "internal", False)
    assert may_send("confidential", "internal", True)


def test_api_config_rejects_unlisted_base_url():
    with pytest.raises(ValueError):
        ApiConfig(base_url="https://proxy.example.com")
    with pytest.raises(ValueError):
        ApiConfig(base_url="http://api.anthropic.com")
    ApiConfig(base_url="https://gw.corp.example", egress_allowlist=("gw.corp.example",))


async def test_egress_guard_blocks_other_hosts():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    guard = EgressGuardTransport(httpx2.MockTransport(handler), frozenset({"api.anthropic.com"}))
    async with httpx2.AsyncClient(transport=guard) as client:
        ok = await client.get("https://api.anthropic.com/v1/models", headers={"x-stainless-os": "MacOS"})
        assert ok.status_code == 200
        assert "x-stainless-os" not in seen[0].headers
        for url in ("https://evil.example.com/", "http://api.anthropic.com/", "https://api.anthropic.com:8443/"):
            with pytest.raises(EgressBlocked):
                await client.get(url)
    assert len(seen) == 1


def test_audit_chain_detects_tampering(isolated_home):
    log = AuditLog()
    log.append("a", value=1)
    log.append("b", key=VALID_KEY)
    log.append("c", value=3)
    ok, count, _ = log.verify()
    assert ok and count == 3
    lines = log.path.read_text().splitlines()
    assert VALID_KEY not in log.path.read_text()
    record = json.loads(lines[1])
    record["data"]["key"] = "changed"
    lines[1] = json.dumps(record, ensure_ascii=False)
    log.path.write_text("\n".join(lines) + "\n")
    ok, _, message = log.verify()
    assert not ok and "변조" in message
    assert_private(log.path, 0o600)


def test_default_settings_are_consistent():
    s = Settings()
    assert s.budget.monthly_soft_usd <= s.budget.monthly_hard_usd
    assert s.models.worker.clamp("max") == "high"
    assert s.models.worker.escalate("high") is None
    assert s.price_for("unknown-model").output == max(p.output for p in s.pricing.values())
