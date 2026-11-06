#!/usr/bin/env python3
"""Read-only, value-free live Router admin credential release gate."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess

CLIENTS = ("assistx-api", "assistx-worker", "auto-assign")
TOKEN_KEY = "AUTO_ROUTER_ADMIN_TOKEN"


def assess(router: str | None, clients: dict[str, str | None],
           source: str | None, source_private: bool) -> dict:
    reasons: list[str] = []
    if not router:
        reasons.append("ROUTER_ADMIN_TOKEN_ABSENT")
    for name in CLIENTS:
        token = clients.get(name)
        if not token:
            reasons.append("CLIENT_TOKEN_ABSENT:" + name)
        elif router and token != router:
            reasons.append("CLIENT_TOKEN_MISMATCH:" + name)
    if not source_private:
        reasons.append("AUTO_ASSIGN_SOURCE_PERMISSIONS_UNSAFE")
    if not source:
        reasons.append("AUTO_ASSIGN_SOURCE_TOKEN_ABSENT")
    elif clients.get("auto-assign") != source:
        reasons.append("AUTO_ASSIGN_DEPLOYED_SOURCE_DRIFT")
    elif router and source != router:
        reasons.append("AUTO_ASSIGN_SOURCE_MISMATCH_ROUTER")
    return {
        "schema": "auto-assign.router-admin-token-release-gate.v1",
        "gate": "HOLD" if reasons else "PASS",
        "reasons": sorted(set(reasons)),
        "checked_clients": list(CLIENTS),
        "no_secrets_or_hashes_exported": True,
        "service_restarted": False,
        "production_state_modified": False,
    }


def inspect_token(container: str) -> str | None:
    process = subprocess.run(
        ["docker", "inspect", "--type=container", container],
        capture_output=True, text=True, timeout=8, check=True,
    )
    data = json.loads(process.stdout)
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("ambiguous container inventory")
    if data[0].get("State", {}).get("Running") is not True:
        raise ValueError("container stopped")
    variables = [
        value.partition("=")[2]
        for value in data[0].get("Config", {}).get("Env", [])
        if isinstance(value, str)
        and value.partition("=")[0] == TOKEN_KEY
        and "=" in value
    ]
    if len(variables) != 1:
        raise ValueError("missing or duplicate admin token")
    return variables[0]


def source_token(path: Path) -> tuple[str | None, bool]:
    if path.is_symlink():
        raise ValueError("secret source is symlink")
    info = path.stat()
    private = stat.S_ISREG(info.st_mode) and not (stat.S_IMODE(info.st_mode) & 0o077)
    values = []
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == TOKEN_KEY:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            values.append(value)
    if len(values) != 1:
        raise ValueError("missing or duplicate source token")
    return values[0], private


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-only", action="store_true", required=True)
    parser.add_argument("--env-file", type=Path,
                        default=Path("/home/scott/git/auto-assign/.env"))
    options = parser.parse_args()
    try:
        router = inspect_token("git-router-1")
        clients = {name: inspect_token(name) for name in CLIENTS}
        source, private = source_token(options.env_file)
        result = assess(router, clients, source, private)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError,
            json.JSONDecodeError):
        result = {"schema": "auto-assign.router-admin-token-release-gate.v1",
                  "gate": "HOLD", "reasons": ["UNVERIFIED_DEPLOYMENT_STATE"],
                  "no_secrets_or_hashes_exported": True,
                  "production_state_modified": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["gate"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
