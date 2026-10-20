from __future__ import annotations

from config import Settings


def test_defaults_include_public_host(monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

    settings = Settings.from_env()

    assert "mcp.188.137.227.14.sslip.io" in settings.allowed_hosts
    assert "https://mcp.188.137.227.14.sslip.io" in settings.allowed_origins


def test_allowed_hosts_env_parsing(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "a, b ,c")

    settings = Settings.from_env()

    assert settings.allowed_hosts == ["a", "b", "c"]


def test_allowed_origins_env_parsing(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "a, b ,c")

    settings = Settings.from_env()

    assert settings.allowed_origins == ["a", "b", "c"]


def test_allowed_hosts_env_drops_empty_entries(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "a,, ,b")

    settings = Settings.from_env()

    assert settings.allowed_hosts == ["a", "b"]
