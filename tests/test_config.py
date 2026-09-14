"""``[loop].home`` and its one override.

The bundle's ``trader.toml`` is read-only inside the Docker image, so the
container needs a way to send the journal and state somewhere other than
whatever the bundle says without editing the file it cannot edit.
``$MERKL_TRADER_HOME`` is that way; this pins that it wins over the config and
that a checkout with nothing set gets exactly what it always got.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from merkl_trader import config as configuration

RAW: dict[str, object] = {
    "agent": {"agent_id": "a", "key_file": "k.pem", "mandate": "m"},
    "treasury": {
        "address": "rTREASURY",
        "policy_version": "1",
        "wallet_file": "w.json",
        "wallet_name": "agent-0",
    },
    "rail": {"json_rpc_url": "https://node.invalid"},
    "market": {
        "base": "XRP",
        "quote_code": "RLUSD",
        "quote_issuer": "rISSUER",
        "depths": ["10"],
        "reference_url": "https://price.invalid",
        "reference_path": "data.amount",
        "reference_source": "a feed",
    },
    "signer": {"url": "https://signer.invalid"},
    "notary": {"url": "https://notary.invalid", "api_key_env": "MERKL_API_KEY_UNSET"},
    "model": {
        "name": "claude-sonnet-5",
        "api_key_env": "ANTHROPIC_API_KEY_UNSET",
        "max_tokens": 4096,
        "usd_per_million_input": "2.00",
        "usd_per_million_output": "10.00",
    },
    "loop": {"interval_seconds": 900, "home": "~/.merkl/trader"},
    "bill": {"bill_day": "monday", "operator": "rOPERATOR"},
}


def test_loop_home_comes_from_the_config_by_default() -> None:
    settings = configuration.parse(RAW)
    assert settings.loop.home == Path("~/.merkl/trader").expanduser()


def test_merkl_trader_home_overrides_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(configuration.TRADER_HOME_ENV, "/var/lib/merkl-trader")
    settings = configuration.parse(RAW)
    assert settings.loop.home == Path("/var/lib/merkl-trader")


def test_an_unset_override_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(configuration.TRADER_HOME_ENV, "")
    settings = configuration.parse(RAW)
    assert settings.loop.home == Path("~/.merkl/trader").expanduser()
