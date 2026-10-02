"""The harness's mounts: the ledger server filtered to reads, CoinGecko optional."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from agents import Agent
from agents.mcp import MCPServerStdio, MCPServerStreamableHttp
from agents.run_context import RunContextWrapper
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from merkl_trader import ledger as books
from merkl_trader.harness import loop
from merkl_trader.harness.loop import (
    COINGECKO_MCP_URL,
    XRPL_READ_TOOLS,
    Harness,
    market_servers,
    xrpl_server,
)
from tests.test_harness import fake_server, settings_for

FAKES = Path(__file__).parent / "fake_market_servers.py"
NODE = "https://node.testnet.invalid:51234"


def fake_xrpl() -> MCPServerStdio:
    return xrpl_server(NODE, command=sys.executable, args=[str(FAKES), "xrpl"])


def fake_coingecko() -> MCPServerStdio:
    return MCPServerStdio(
        params={"command": sys.executable, "args": [str(FAKES), "coingecko"]},
        name="coingecko",
        client_session_timeout_seconds=20,
    )


async def tool_names(server: Any) -> list[str]:
    agent = Agent(name="t")
    return sorted(t.name for t in await server.list_tools(RunContextWrapper(None), agent))


@pytest.mark.asyncio
async def test_the_ledger_server_lists_exactly_the_four_reads() -> None:
    async with fake_xrpl() as server:
        names = await tool_names(server)

    assert names == sorted(XRPL_READ_TOOLS)
    assert names == [
        "get_account_info",
        "get_account_lines",
        "get_book_offers",
        "get_transaction_info",
    ]
    assert "submit_transaction" not in names


def test_the_real_ledger_server_is_configured_from_the_rail_url() -> None:
    server = xrpl_server(NODE)

    assert server.params.args == ["-m", "xrpl_mcp_server"]
    assert server.params.env == {"XRPL_NODE_URL": NODE}
    assert server.tool_filter == {"allowed_tool_names": list(XRPL_READ_TOOLS)}


def test_market_servers_mount_the_ledger_and_coingecko(tmp_path: Path) -> None:
    servers = market_servers(settings_for(tmp_path))

    assert [s.name for s in servers] == ["xrpl-mcp-server", "coingecko"]
    assert isinstance(servers[1], MCPServerStreamableHttp)
    assert servers[1].params["url"] == COINGECKO_MCP_URL == "https://mcp.api.coingecko.com/mcp"


def test_coingecko_can_be_turned_off(tmp_path: Path) -> None:
    from merkl_trader import config as configuration
    from tests.test_harness import RAW

    settings = configuration.parse({**RAW, "harness": {"coingecko": False}})

    assert [s.name for s in market_servers(settings)] == ["xrpl-mcp-server"]


@pytest.mark.asyncio
async def test_the_agent_sees_every_server_filtered_and_can_read_the_book(
    tmp_path: Path,
) -> None:
    seen: dict[str, list[str]] = {}

    class Spy(ScriptedModel):
        async def get_response(self, system_instructions, *args, **kwargs):  # type: ignore[no-untyped-def]
            tools = kwargs.get("tools") or args[2]
            seen.setdefault("tools", sorted(getattr(t, "name", "") for t in tools))
            return await super().get_response(system_instructions, *args, **kwargs)

    model = Spy(
        [
            ModelStep(
                output=[
                    function_call(
                        "get_book_offers",
                        {"taker_gets": {"currency": "XRP"}, "taker_pays": {"currency": "USD"}},
                        call_id="c1",
                    )
                ]
            ),
            ModelStep(output=[assistant_message("book is thin, holding")]),
        ]
    )
    settings = settings_for(tmp_path)
    journal = books.Journal(settings.loop.journal_md, settings.loop.journal_jsonl)
    async with fake_server(tmp_path, {}) as merkl, fake_xrpl() as xrpl, fake_coingecko() as cg:
        harness = Harness(
            settings, journal=journal, server=merkl, extra_servers=[xrpl, cg], model=model
        )
        entry = await harness.one_cycle()

    assert entry.action == "hold"
    assert "book is thin" in entry.headline
    assert "submit_transaction" not in seen["tools"]
    assert {"get_book_offers", "get_price", "propose_swap", "verify_receipt"} <= set(seen["tools"])
    assert "get_market" not in seen["tools"]
    assert "web_search" in " ".join(seen["tools"]) or any("search" in n for n in seen["tools"])
    model.assert_complete()


def test_the_system_prompt_names_the_sources() -> None:
    prompt = loop.system_prompt("m", operator="rOP", bill_day="monday")

    assert "get_book_offers" in prompt
    assert "CoinGecko" in prompt
    assert "web search" in prompt.lower()
    assert "Money only goes through Merkl" in prompt
    assert "get_market" not in prompt


def _real_ledger_python() -> str | None:
    command, _ = loop.xrpl_command()
    probe = "from xrpl_mcp_server.server import mcp"
    done = subprocess.run([command, "-c", probe], capture_output=True, timeout=30, check=False)
    return command if done.returncode == 0 else None


def test_the_ledger_command_resolves_to_an_interpreter_that_has_the_server() -> None:
    command, args = loop.xrpl_command()

    assert args == ["-m", "xrpl_mcp_server"]
    if _real_ledger_python() is None:
        pytest.skip("no interpreter with xrpl_mcp_server and mcp<2 (set XRPL_MCP_PYTHON)")
    assert os.path.exists(command)


@pytest.mark.asyncio
async def test_the_real_ledger_server_answers_initialize_and_lists_the_reads() -> None:
    if _real_ledger_python() is None:
        pytest.skip("no interpreter with xrpl_mcp_server and mcp<2 (set XRPL_MCP_PYTHON)")
    async with xrpl_server("https://s.altnet.rippletest.net:51234/") as server:
        names = await tool_names(server)

    assert names == sorted(XRPL_READ_TOOLS)
    assert "get_book_offers" in names


@pytest.mark.asyncio
async def test_a_dropped_market_server_is_loud_in_the_journal(tmp_path: Path) -> None:
    model = ScriptedModel([ModelStep(output=[assistant_message("holding")])])
    settings = settings_for(tmp_path)
    journal = books.Journal(settings.loop.journal_md, settings.loop.journal_jsonl)
    async with fake_server(tmp_path, {}) as merkl:
        harness = Harness(
            settings,
            journal=journal,
            server=merkl,
            model=model,
            unavailable=["ledger server unavailable: Connection closed"],
        )
        entry = await harness.one_cycle()

    assert entry.headline.startswith("ledger server unavailable: Connection closed.")
