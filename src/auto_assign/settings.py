from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("AUTO_ASSIGN_HOST", "0.0.0.0")
    port: int = _int_env("AUTO_ASSIGN_PORT", 8090)
    database_url: str = os.getenv("AUTO_ASSIGN_DATABASE_URL", "sqlite:///./data/auto_assign.sqlite3")
    assistx_base_url: str = os.getenv("AUTO_ASSIGN_ASSISTX_BASE_URL", "http://localhost:8000")
    assistx_timeout_seconds: int = _int_env("AUTO_ASSIGN_ASSISTX_TIMEOUT_SECONDS", 5)
    router_base_url: str = os.getenv("AUTO_ASSIGN_ROUTER_BASE_URL", "http://localhost:8088")
    router_timeout_seconds: int = _int_env("AUTO_ASSIGN_ROUTER_TIMEOUT_SECONDS", 5)
    scheduler_enabled: bool = _bool_env("AUTO_ASSIGN_SCHEDULER_ENABLED", False)
    tick_interval_seconds: int = _int_env("AUTO_ASSIGN_TICK_INTERVAL_SECONDS", 300)
    default_lease_seconds: int = _int_env("AUTO_ASSIGN_DEFAULT_LEASE_SECONDS", 900)
    stale_heartbeat_seconds: int = _int_env("AUTO_ASSIGN_STALE_HEARTBEAT_SECONDS", 120)
    dispatch_enabled: bool = _bool_env("AUTO_ASSIGN_DISPATCH_ENABLED", False)
    direct_workers_enabled: bool = _bool_env("AUTO_ASSIGN_DIRECT_WORKERS_ENABLED", False)
    log_payload_bodies: bool = _bool_env("AUTO_ASSIGN_LOG_PAYLOAD_BODIES", False)

    @property
    def sqlite_path(self) -> Path:
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("Only sqlite:/// URLs are supported for the local cache layer")
        path_text = self.database_url.removeprefix("sqlite:///")
        return Path(path_text).expanduser().resolve()


def get_settings() -> Settings:
    return Settings()
