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


@dataclass
class Settings:
    db_path: str = "./mcp.db"
    provider: str = "mock"
    fivesim_token: str = ""
    markup: float = 2.5
    rate_limit_per_15min: int = 20
    daily_code_limit: int = 200
    code_wait_seconds: int = 120
    usd_kop_rate: int = 9200
    host: str = "127.0.0.1"
    port: int = 8765
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        provider = os.environ.get("PROVIDER", "mock").strip().lower()
        if provider not in {"mock", "fivesim"}:
            provider = "mock"
        return cls(
            db_path=os.environ.get("MCP_DB", "./mcp.db"),
            provider=provider,
            fivesim_token=os.environ.get("FIVESIM_TOKEN", "").strip(),
            markup=_env_float("MCP_MARKUP", 2.5),
            rate_limit_per_15min=_env_int("RATE_LIMIT_PER_15MIN", 20),
            daily_code_limit=_env_int("DAILY_CODE_LIMIT", 200),
            code_wait_seconds=_env_int("CODE_WAIT_SECONDS", 120),
            usd_kop_rate=_env_int("USD_KOP_RATE", 9200),
        )
