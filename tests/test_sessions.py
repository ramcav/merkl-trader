"""A cycle is one Merkl session: opened, every tool call recorded, sealed."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from merkl_trader import ledger as books
from merkl_trader.harness import sessions
from merkl_trader.harness.loop import Harness
from tests.test_harness import TIMEOUT, fake_server, settings_for


class FakeSession:
    def __init__(self, goal: str, allowed_tools: list[str]) -> None:
        self.goal = goal
        self.allowed_tools = allowed_tools
        self.session_id: str | None = None
        self.action_count = 0
        self.actions: list[dict[str, Any]] = []
        self.sealed = False

    async def __aenter__(self) -> FakeSession:
        self.session_id = "sess-test"
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.sealed = True

    async def record_action(self, **kwargs: Any) -> dict[str, Any]:
        self.actions.append(kwargs)
        self.action_count += 1
        return {"action_id": f"act-{self.action_count}"}


class FakeClient:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    def session(self, goal: str, allowed_tools: list[str] | None = None) -> FakeSession:
        made = FakeSession(goal, allowed_tools or [])
        self.sessions.append(made)
        return made


async def cycle(tmp_path: Path, model: ScriptedModel, script: dict[str, Any], client: Any):
    settings = settings_for(tmp_path)
    journal = books.Journal(settings.loop.journal_md, settings.loop.journal_jsonl)
    server = fake_server(tmp_path, script)
    async with server:
        harness = Harness(settings, journal=journal, server=server, model=model, client=client)
        return await asyncio.wait_for(harness.one_cycle(), timeout=TIMEOUT)


SWAP = {
    "sell_amount": "4",
    "sell_currency": "XRP",
    "buy_amount": "2",
    "buy_currency": "RLUSD",
    "why": "inside my limit",
}


@pytest.mark.asyncio
async def test_a_cycle_opens_one_session_records_its_actions_and_seals_it(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            ModelStep(output=[function_call("get_treasury", {}, call_id="c1")]),
            ModelStep(output=[function_call("read_receipts", {}, call_id="c2")]),
            ModelStep(output=[assistant_message("nothing worth doing")]),
        ]
    )
    client = FakeClient()

    entry = await cycle(tmp_path, model, {}, client)

    (session,) = client.sessions
    assert entry.action == "hold"
    assert session.goal == "Grow the treasury."
    assert {"get_treasury", "propose_swap", "web_search"} <= set(session.allowed_tools)
    names = [a["tool_name"] for a in session.actions]
    assert names == ["get_treasury", "read_receipts", "agent.final_text"]
    assert session.actions[1]["depends_on"] == ["act-1"]
    assert "nothing worth doing" in session.actions[-1]["output_data"]
    assert session.sealed


@pytest.mark.asyncio
async def test_the_proposal_carries_the_session_id_without_the_model_knowing(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        [
            ModelStep(output=[function_call("get_treasury", {}, call_id="c1")]),
            ModelStep(output=[function_call("propose_swap", SWAP, call_id="c2")]),
        ]
    )
    client = FakeClient()

    entry = await cycle(tmp_path, model, {"propose": {"outcome": "settled"}}, client)

    (session,) = client.sessions
    assert '"joined_session": "sess-test"' in entry.headline
    assert '"session_action_count": 1' in entry.headline  # get_treasury was action 1
    assert '"depends_on": "act-1"' in entry.headline
    assert [a["tool_name"] for a in session.actions] == ["get_treasury"], (
        "the receipt is the proposal's action; the harness does not record it twice"
    )
    assert session.sealed


@pytest.mark.asyncio
async def test_the_model_never_sees_the_session_arguments(tmp_path: Path) -> None:
    server = fake_server(tmp_path, {})
    async with server:
        tools = {tool.name: tool for tool in await server.list_tools()}

    properties = tools["propose_swap"].input_schema["properties"]
    assert "sell_amount" in properties
    assert not {"session_id", "session_action_count", "depends_on"} & set(properties)


@pytest.mark.asyncio
async def test_a_failing_run_still_seals_the_session(tmp_path: Path) -> None:
    model = ScriptedModel([ModelStep.raise_error(RuntimeError("401 Unauthorized"))])
    client = FakeClient()

    entry = await cycle(tmp_path, model, {}, client)

    (session,) = client.sessions
    assert "401 Unauthorized" in entry.headline
    assert session.sealed


@pytest.mark.asyncio
async def test_a_notary_that_cannot_open_a_session_costs_the_session_not_the_cycle(
    tmp_path: Path,
) -> None:
    class Broken:
        def session(self, **_kwargs: Any) -> Any:
            raise RuntimeError("notary down")

    model = ScriptedModel([ModelStep(output=[assistant_message("holding")])])

    entry = await cycle(tmp_path, model, {}, Broken())

    assert entry.action == "hold" and "holding" in entry.headline


def test_outputs_are_cut_to_four_kilobytes_and_the_goal_is_the_first_sentence() -> None:
    assert len(sessions.clip("x" * 10_000)) < 4_200
    assert sessions.clip({"a": 1}) == '{"a": 1}'
    assert sessions.goal_of("Grow the treasury. Prefer cash.") == "Grow the treasury."
