"""Synthetic fixtures only: never connect to production Docker."""
import json
import os

import pytest
from scripts.router_admin_token_release_gate import (
    CLIENTS,
    assess,
    source_token,
)


def synthetic_clients(value="a-private-test-token"):
    return {name: value for name in CLIENTS}


def test_aligned_private_source_passes():
    result = assess("a-private-test-token", synthetic_clients(),
                    "a-private-test-token", True)
    assert result["gate"] == "PASS"
    assert not result["reasons"]
    assert not result["production_state_modified"]


def test_stale_autoassign_blocks_even_if_other_clients_match():
    clients = synthetic_clients()
    clients["auto-assign"] = "stale-test-token"
    result = assess("a-private-test-token", clients,
                    "stale-test-token", True)
    assert result["gate"] == "HOLD"
    assert "CLIENT_TOKEN_MISMATCH:auto-assign" in result["reasons"]
    assert "AUTO_ASSIGN_SOURCE_MISMATCH_ROUTER" in result["reasons"]


def test_saved_source_drift_blocks():
    result = assess("a-private-test-token", synthetic_clients(),
                    "stale-test-token", True)
    assert "AUTO_ASSIGN_DEPLOYED_SOURCE_DRIFT" in result["reasons"]


def test_missing_and_unsafe_sources_block():
    result = assess("a-private-test-token", synthetic_clients(),
                    None, False)
    assert "AUTO_ASSIGN_SOURCE_TOKEN_ABSENT" in result["reasons"]
    assert "AUTO_ASSIGN_SOURCE_PERMISSIONS_UNSAFE" in result["reasons"]


def test_missing_client_or_router_blocks():
    clients = synthetic_clients()
    clients["assistx-worker"] = None
    result = assess(None, clients, "a-private-test-token", True)
    assert "ROUTER_ADMIN_TOKEN_ABSENT" in result["reasons"]
    assert "CLIENT_TOKEN_ABSENT:assistx-worker" in result["reasons"]


def test_no_value_or_digest_leaks_in_result():
    token = "SYNTHETIC_SECRET_NEVER_LOG_ME"
    result = assess(token, synthetic_clients("different-token"),
                    "different-source", True)
    data = json.dumps(result)
    assert token not in data
    assert "different-token" not in data
    assert "different-source" not in data
    assert result["no_secrets_or_hashes_exported"]


def test_secure_dotenv_source_reads_value_only_locally(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nAUTO_ROUTER_ADMIN_TOKEN="synthetic=token"\n')
    os.chmod(p, 0o600)
    token, private = source_token(p)
    assert token == "synthetic=token"
    assert private


def test_world_readable_dotenv_is_not_private(tmp_path):
    p = tmp_path / ".env"
    p.write_text("AUTO_ROUTER_ADMIN_TOKEN=synthetic\n")
    os.chmod(p, 0o644)
    assert source_token(p)[1] is False


def test_duplicated_admin_keys_are_rejected(tmp_path):
    p = tmp_path / ".env"
    p.write_text("AUTO_ROUTER_ADMIN_TOKEN=a\nAUTO_ROUTER_ADMIN_TOKEN=b\n")
    os.chmod(p, 0o600)
    with pytest.raises(ValueError):
        source_token(p)


def test_symlinked_source_is_rejected(tmp_path):
    actual = tmp_path / "real"
    actual.write_text("AUTO_ROUTER_ADMIN_TOKEN=synthetic\n")
    os.chmod(actual, 0o600)
    alias = tmp_path / "link"
    alias.symlink_to(actual)
    with pytest.raises(ValueError):
        source_token(alias)


def test_local_dev_auth_bypass_blocks_release():
    aligned = "a-private-test-token"
    result = assess(
        aligned, synthetic_clients(aligned), aligned, True,
        allow_unauthenticated_local_dev=True,
    )
    assert result["gate"] == "HOLD"
    assert "AUTO_ASSIGN_UNAUTHENTICATED_DEV_BYPASS_ENABLED" in result["reasons"]
    assert aligned not in json.dumps(result)
