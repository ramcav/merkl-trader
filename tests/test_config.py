"""``[loop].home`` and its one override, and ``[model].provider``.

The bundle's ``trader.toml`` is read-only inside the Docker image, so the
container needs a way to send the journal and state somewhere other than
whatever the bundle says without editing the file it cannot edit.
``$MERKL_TRADER_HOME`` is that way; this pins that it wins over the config and
that a checkout with nothing set gets exactly what it always got.

``[model].provider`` gets the same "an old config is unchanged" treatment:
optional, defaulting to ``"anthropic"``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

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


def _with_model(**overrides: Any) -> dict[str, object]:
    raw = copy.deepcopy(RAW)
    raw["model"].update(overrides)  # type: ignore[union-attr]
    return raw


def test_provider_defaults_to_anthropic_for_an_old_config() -> None:
    """RAW's [model] names no provider at all — a config written before this existed."""
    settings = configuration.parse(RAW)
    assert settings.model.provider == "anthropic"


def test_provider_can_be_named_explicitly() -> None:
    settings = configuration.parse(_with_model(provider="anthropic"))
    assert settings.model.provider == "anthropic"

    settings = configuration.parse(_with_model(provider="openai", name="gpt-5.4-mini"))
    assert settings.model.provider == "openai"


def test_an_unknown_provider_is_a_configerror() -> None:
    with pytest.raises(configuration.ConfigError, match="model.provider"):
        configuration.parse(_with_model(provider="gemini"))


# --------------------------------------------------------------------------- #
# Every path in the config, anchored to trader.toml's own directory — not the
# process's working directory. This is the production bug: a container
# started as `--config /agent/trader.toml` has no reason to share a working
# directory with /agent, and `key_file = "agent-ed25519.pem"` (relative, by
# design, so the bundle can move) must still resolve.
# --------------------------------------------------------------------------- #


def _toml(document: dict[str, dict[str, object]]) -> str:
    """A minimal, purpose-built TOML writer — just enough for this file's tables."""
    lines: list[str] = []
    for section, fields in document.items():
        lines.append(f"[{section}]")
        for key, value in fields.items():
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, int):
                lines.append(f"{key} = {value}")
            elif isinstance(value, list):
                items = ", ".join(f'"{entry}"' for entry in value)
                lines.append(f"{key} = [{items}]")
            else:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"')
        lines.append("")
    return "\n".join(lines)


def _bundle_document(
    *, key_file: str, wallet_file: str, token_file: str, api_key_file: str, home: str
) -> dict[str, dict[str, object]]:
    document = copy.deepcopy(RAW)
    document["agent"]["key_file"] = key_file  # type: ignore[index]
    document["treasury"]["wallet_file"] = wallet_file  # type: ignore[index]
    document["signer"]["token_file"] = token_file  # type: ignore[index]
    document["notary"]["api_key_file"] = api_key_file  # type: ignore[index]
    document["loop"]["home"] = home  # type: ignore[index]
    return document  # type: ignore[return-value]


def test_relative_paths_resolve_against_the_configs_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "agent"
    bundle.mkdir()
    (bundle / "trader.toml").write_text(
        _toml(
            _bundle_document(
                key_file="agent-ed25519.pem",
                wallet_file="wallet.json",
                token_file="relay-token.txt",
                api_key_file="notary-api-key.txt",
                home="state",
            )
        )
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # the container's cwd is never the bundle's directory

    settings = configuration.load(bundle / "trader.toml")

    assert settings.agent.key_file == bundle / "agent-ed25519.pem"
    assert settings.treasury.wallet_file == bundle / "wallet.json"
    assert settings.signer.token_file == bundle / "relay-token.txt"
    assert settings.notary.api_key_file == bundle / "notary-api-key.txt"
    assert settings.loop.home == bundle / "state"


def test_absolute_paths_are_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "agent"
    bundle.mkdir()
    secrets = tmp_path / "secrets-elsewhere"
    secrets.mkdir()
    (bundle / "trader.toml").write_text(
        _toml(
            _bundle_document(
                key_file=str(secrets / "agent-ed25519.pem"),
                wallet_file=str(secrets / "wallet.json"),
                token_file=str(secrets / "relay-token.txt"),
                api_key_file=str(secrets / "notary-api-key.txt"),
                home=str(tmp_path / "state"),
            )
        )
    )
    monkeypatch.chdir(tmp_path)

    settings = configuration.load(bundle / "trader.toml")

    assert settings.agent.key_file == secrets / "agent-ed25519.pem"
    assert settings.treasury.wallet_file == secrets / "wallet.json"
    assert settings.signer.token_file == secrets / "relay-token.txt"
    assert settings.notary.api_key_file == secrets / "notary-api-key.txt"
    assert settings.loop.home == tmp_path / "state"


def test_tilde_paths_still_expand_instead_of_joining_the_configs_directory(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "agent"
    bundle.mkdir()
    (bundle / "trader.toml").write_text(
        _toml(
            _bundle_document(
                key_file="~/agent-ed25519.pem",
                wallet_file="wallet.json",  # relative, alongside the ~ path, in the same file
                token_file="~/relay-token.txt",
                api_key_file="notary-api-key.txt",
                home="~/.merkl/trader",
            )
        )
    )

    settings = configuration.load(bundle / "trader.toml")

    assert settings.agent.key_file == Path("~/agent-ed25519.pem").expanduser()
    assert settings.signer.token_file == Path("~/relay-token.txt").expanduser()
    assert settings.loop.home == Path("~/.merkl/trader").expanduser()
    # relative paths in the very same file still anchor to the bundle, not $HOME
    assert settings.treasury.wallet_file == bundle / "wallet.json"
    assert settings.notary.api_key_file == bundle / "notary-api-key.txt"
