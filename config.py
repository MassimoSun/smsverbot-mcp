from __future__ import annotations

import os
from dataclasses import dataclass, field


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip("'\"")
            if name:
                os.environ.setdefault(name, value)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None:
        return list(default)
    items = [item.strip() for item in raw.split(",")]
    return [item for item in items if item]


DEFAULT_ALLOWED_HOSTS = [
    "127.0.0.1:8765",
    "localhost:8765",
    "kremledroid.duckdns.org",
    "kremledroid.duckdns.org:443",
    "mcp.188.137.227.14.sslip.io",
    "mcp.188.137.227.14.sslip.io:443",
]
DEFAULT_ALLOWED_ORIGINS = [
    "https://kremledroid.duckdns.org",
    "https://mcp.188.137.227.14.sslip.io",
]


@dataclass
class Settings:
    db_path: str = "./mcp.db"
    default_api_key: str = ""
    provider: str = "mock"
    fivesim_token: str = ""
    markup: float = 2.5
    rate_limit_per_15min: int = 20
    daily_code_limit: int = 200
    daily_spend_limit_rub: int = 1000
    max_waits_per_key: int = 2
    max_waits_global: int = 50
    code_wait_seconds: int = 120
    usd_kop_rate: int = 9200
    host: str = "127.0.0.1"
    port: int = 8765
    wallet_url: str = "http://127.0.0.1:8767"
    wallet_token: str = ""
    auto_cancel_interval: int = 60
    sandbox_daily_limit: int = 50
    sandbox_code_delay: int = 4
    allowed_hosts: list[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_HOSTS)
    )
    allowed_origins: list[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_ORIGINS)
    )
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        provider = os.environ.get("PROVIDER", "mock").strip().lower()
        if provider not in {"mock", "fivesim"}:
            provider = "mock"
        return cls(
            db_path=os.environ.get("MCP_DB", "./mcp.db"),
            default_api_key=os.environ.get("MCP_API_KEY", "").strip(),
            provider=provider,
            fivesim_token=os.environ.get("FIVESIM_TOKEN", "").strip(),
            markup=_env_float("MCP_MARKUP", 2.5),
            rate_limit_per_15min=_env_int("RATE_LIMIT_PER_15MIN", 20),
            daily_code_limit=_env_int("DAILY_CODE_LIMIT", 200),
            daily_spend_limit_rub=_env_int("DAILY_SPEND_LIMIT_RUB", 1000),
            max_waits_per_key=_env_int("MAX_WAITS_PER_KEY", 2),
            max_waits_global=_env_int("MAX_WAITS_GLOBAL", 50),
            code_wait_seconds=_env_int("CODE_WAIT_SECONDS", 120),
            usd_kop_rate=_env_int("USD_KOP_RATE", 9200),
            wallet_url=os.environ.get("WALLET_URL", "http://127.0.0.1:8767").strip()
            or "http://127.0.0.1:8767",
            wallet_token=os.environ.get("WALLET_TOKEN", "").strip(),
            auto_cancel_interval=_env_int("AUTO_CANCEL_INTERVAL", 60),
            sandbox_daily_limit=_env_int("SANDBOX_DAILY_LIMIT", 50),
            sandbox_code_delay=_env_int("SANDBOX_CODE_DELAY", 4),
            allowed_hosts=_env_list("MCP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS),
            allowed_origins=_env_list("MCP_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS),
        )
