"""The harness, end to end against a real ``merkl-mcp`` stdio subprocess.

``fake_mcp_server.py`` (this directory) speaks Phase 23's shared contract —
the six tools the brief's table names — so ``MCPServerStdio`` talks to a real
process over a real stdio handshake, the same as it would to the real
``merkl-mcp``. The only other thing stubbed is the model, with the OpenAI
Agents SDK's own ``agents.testing.ScriptedModel`` — no key, no network, same
spirit as ``test_trader.py``'s ``ScriptedModel`` for the Anthropic/OpenAI
loop.

What this suite asserts: the model gets real tools and ends its turn the
instant a ``propose_*`` answers (never a second action in the same cycle); a
human still deciding means the model is not asked at all; a resolved
escalation is journaled without asking the model either; and a model or
transport failure is a hold with a note, never a crash.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from agents.mcp import MCPServerStdio
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from merkl_trader import config as configuration
from merkl_trader import ledger as books
from merkl_trader.harness.loop import Harness

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"
TIMEOUT = 20
"""Every awaited call in this file is bounded, so a hung subprocess fails the
test instead of the suite."""

RAW: dict[str, Any] = {
    "agent": {"agent_id": "agent-trader", "key_file": "k.pem", "mandate": "Grow the treasury."},
    "treasury": {
        "address": "rTREASURYexampleaccount0000000000",
        "policy_version": "2026.09.16",
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
        "provider": "openai",
        "name": "gpt-5.4-mini",
        "api_key_env": "OPENAI_API_KEY_UNSET",
        "max_tokens": 4096,
        "usd_per_million_input": "2.00",
        "usd_per_million_output": "10.00",
    },
    "loop": {"interval_seconds": 900, "home": "~/.merkl/trader"},
    "bill": {"bill_day": "monday", "operator": "rOPERATORexampleaccount000000000"},
}


def settings_for(home: Path) -> configuration.Config:
    return configuration.parse({**RAW, "loop": {"interval_seconds": 900, "home": str(home)}})


def fake_server(home: Path, script: dict[str, Any]) -> MCPServerStdio:
    script_path = home / "fake-mcp-script.json"
    script_path.write_text(json.dumps(script))
    return MCPServerStdio(
        params={
            "command": sys.executable,
            "args": [str(FAKE_SERVER)],
            "env": {"FAKE_MCP_SCRIPT": str(script_path)},
        },
        name="fake-merkl-mcp",
        client_session_timeout_seconds=TIMEOUT,
    )


async def one_cycle(
    tmp_path: Path, *, script: dict[str, Any], model: ScriptedModel
) -> tuple[books.Entry, Harness]:
    settings = settings_for(tmp_path)
    journal = books.Journal(settings.loop.journal_md, settings.loop.journal_jsonl)
    server = fake_server(tmp_path, script)
    async with server:
        harness = Harness(settings, journal=journal, server=server, model=model)
        entry = await asyncio.wait_for(harness.one_cycle(), timeout=TIMEOUT)
        return entry, harness


# --------------------------------------------------------------------------- #
# 1. It trades — a swap settles, and the run stops at the one proposal.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_cycle_that_settles_a_swap(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelStep(output=[function_call("get_treasury", {}, call_id="c1")]),
            ModelStep(
                output=[
                    assistant_message("Book looks fine."),
                    function_call(
                        "propose_swap",
                        {
                            "sell_amount": "4",
                            "sell_currency": "XRP",
                            "buy_amount": "2",
                            "buy_currency": "RLUSD",
                            "why": "the spread was inside my limit",
                        },
                        call_id="c2",
                    ),
                ]
            ),
        ]
    )
    script = {
        "treasury": {
            "address": "rTREASURY",
            "network": "xrpl",
            "balances": {"XRP": "500", "RLUSD": "40"},
            "policy_version": "1",
            "signer_health": "ok",
            "pending_with_a_person": False,
        },
        "propose": {"outcome": "settled", "tx_hash": "deadbeef"},
    }
    entry, _ = await one_cycle(tmp_path, script=script, model=model)

    assert entry.action == "swap"
    assert entry.outcome == "settled"
    assert entry.balances == {"XRP": pytest.approx(500), "RLUSD": pytest.approx(40)}
    assert "propose_swap" in entry.headline
    model.assert_complete()


@pytest.mark.asyncio
async def test_only_one_proposal_reaches_the_server_per_cycle(tmp_path: Path) -> None:
    """A model that keeps calling tools after a proposal never gets a second
    one: ``stop_at_tool_names`` ends the run on the first ``propose_*``."""
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        "propose_swap",
                        {
                            "sell_amount": "4",
                            "sell_currency": "XRP",
                            "buy_amount": "2",
                            "buy_currency": "RLUSD",
                            "why": "first",
                        },
                        call_id="c1",
                    )
                ]
            ),
            ModelStep(output=[function_call("get_market", {}, call_id="c2")]),  # never run
        ]
    )
    script = {"propose": {"outcome": "settled", "tx_hash": "abc"}}
    entry, _ = await one_cycle(tmp_path, script=script, model=model)

    assert entry.outcome == "settled"
    assert model.remaining_steps == 1, "the second scripted turn was never asked for"


# --------------------------------------------------------------------------- #
# 2. It hears a refusal
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_refusal_is_journaled_in_the_rules_own_words(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        "propose_payment",
                        {
                            "destination": "rOPERATOR",
                            "amount": "9999",
                            "currency": "XRP",
                            "why": "testing the cap",
                        },
                        call_id="c1",
                    )
                ]
            )
        ]
    )
    script = {"propose": {"outcome": "refused", "rule": "per_tx_cap: over 5 XRP"}}
    entry, _ = await one_cycle(tmp_path, script=script, model=model)

    assert entry.action == "payment"
    assert entry.outcome == "denied"
    assert "per_tx_cap" in entry.headline


# --------------------------------------------------------------------------- #
# 3. It waits for a human, and journals a resolution without asking the model
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_model_is_not_asked_while_a_human_is_deciding(tmp_path: Path) -> None:
    model = ScriptedModel([])  # any get_response call fails the test
    script = {"pending_sequence": [{"status": "waiting", "time_left_seconds": 300}]}
    entry, _ = await one_cycle(tmp_path, script=script, model=model)

    assert entry.action == "wait"
    assert entry.outcome == "escalated"
    assert "Waiting on a human" in entry.headline
    model.assert_complete()


@pytest.mark.asyncio
async def test_a_resolved_escalation_is_journaled_without_asking_the_model(
    tmp_path: Path,
) -> None:
    model = ScriptedModel([])
    script = {"pending_sequence": [{"status": "settled", "tx_hash": "abc123"}]}
    entry, _ = await one_cycle(tmp_path, script=script, model=model)

    assert entry.outcome == "settled"
    assert "answered an earlier proposal" in entry.headline
    model.assert_complete()


# --------------------------------------------------------------------------- #
# 4. It never crashes: a bad key, no network, or an over-long turn is a hold
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_holding_is_a_real_outcome(tmp_path: Path) -> None:
    model = ScriptedModel([ModelStep(output=[assistant_message("quiet market, holding")])])
    entry, _ = await one_cycle(tmp_path, script={}, model=model)

    assert entry.action == "hold"
    assert entry.outcome == "none"
    assert "quiet market, holding" in entry.headline


@pytest.mark.asyncio
async def test_a_failed_model_call_is_a_clean_hold(tmp_path: Path) -> None:
    model = ScriptedModel([ModelStep.raise_error(RuntimeError("401 Unauthorized"))])
    entry, _ = await one_cycle(tmp_path, script={}, model=model)

    assert entry.action == "hold"
    assert "401 Unauthorized" in entry.headline


@pytest.mark.asyncio
async def test_a_turn_that_never_proposes_is_cut_off_as_a_hold(tmp_path: Path) -> None:
    from merkl_trader.harness.loop import MAX_TURNS

    steps = [
        ModelStep(output=[function_call("get_treasury", {}, call_id=f"c{i}")])
        for i in range(MAX_TURNS + 2)
    ]
    model = ScriptedModel(steps)
    entry, _ = await one_cycle(tmp_path, script={}, model=model)

    assert entry.action == "hold"
    assert "still reading" in entry.headline


# --------------------------------------------------------------------------- #
# 5. The journal, and the two-line shape it writes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_journal_gets_the_same_two_lines_a_cycle(tmp_path: Path) -> None:
    model = ScriptedModel([ModelStep(output=[assistant_message("nothing worth doing")])])
    entry, harness = await one_cycle(tmp_path, script={}, model=model)
    first, second = harness.journal.append(entry)

    assert "runway unknown" in first
    assert "nothing worth doing" in second
    assert (harness.settings.loop.home / "journal.md").read_text().count("\n\n") >= 1


# --------------------------------------------------------------------------- #
# 6. The system prompt states the mandate and never a policy number
# --------------------------------------------------------------------------- #


def test_the_system_prompt_states_the_mandate_and_no_policy_number() -> None:
    from merkl_trader.harness.loop import system_prompt

    prompt = system_prompt(
        "Trade the treasury's XRP against RLUSD. Prefer doing nothing to a thin book.",
        operator="rOPERATORexampleaccount000000000",
        bill_day="monday",
    )
    assert "Trade the treasury's XRP against RLUSD" in prompt
    assert "rOPERATORexampleaccount000000000" in prompt
    assert "you do not control" in prompt
    assert "information, not a failure" in prompt
    for forbidden in ("per_tx_cap", "5 XRP", "threshold of", "window of"):
        assert forbidden not in prompt
