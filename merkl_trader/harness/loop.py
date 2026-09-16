"""The harness loop: wake up, run one OpenAI Agents SDK agent, write it down.

Phase 23, section B — "Merkl as a tool server, and an agent with real
tooling." ``trader.py`` reads the ledger itself and holds the receipt builder
itself; this process holds neither. The market, the treasury, the receipts and
the one-proposal-at-a-time rule all live behind ``merkl-mcp``
(https://github.com/ramcav/merkl-mcp), mounted over stdio as a tool server any
harness can use — the same shared contract a Claude Code or Hermes mount would
see. What is left here is thin on purpose: wake up, hand the model its mandate
and its tools (``merkl-mcp`` for anything money-shaped, ``WebSearchTool`` for
the outside world), wait for at most one proposal or a hold, journal it,
sleep. There is no local ``State``, no nonce, no in-flight bookkeeping — that
crash-safety already lives behind ``merkl-mcp``'s own ``ReceiptBuilder``,
exactly as it does for the no-framework loop in ``trader.py``. There is also
no compute-bill accrual here: ``get_treasury`` names no owed amount in the
shared contract, so unlike ``ledger.Bill`` this agent is only ever told a
bill exists and who it is paid to, never a number to pay — it has to reason
about the mandate the way it reasons about a policy it cannot read.

One cap is structural, not a rule the model is asked to respect:
``Agent.tool_use_behavior={"stop_at_tool_names": PROPOSAL_TOOLS}`` ends the
run the instant ``propose_swap`` or ``propose_payment`` returns, the same way
``decide.py``'s hand-rolled loop stops at the first ``tool_use`` block that
is one of the two action tools. A second guard lives in ``merkl-mcp`` itself
(a ``propose_*`` call while one is already waiting on a person returns
``waiting_for_a_person`` without proposing), and a third in this loop, which
calls the model at most once per wake-up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Protocol

from agents import (
    Agent,
    ModelSettings,
    Runner,
    RunResult,
    WebSearchTool,
    set_default_openai_key,
    set_tracing_disabled,
)
from agents.exceptions import AgentsException, MaxTurnsExceeded
from agents.items import RunItem, ToolCallItem, ToolCallOutputItem
from agents.mcp import MCPServer, MCPServerStdio

from merkl_trader import config as configuration
from merkl_trader import ledger as books

MERKL_MCP_COMMAND: Final = "merkl-mcp"
"""The console script ``merkl-mcp``'s own ``pyproject.toml`` installs."""

PROPOSAL_TOOLS: Final = ("propose_payment", "propose_swap")

MAX_TURNS: Final = 10
"""Tool-call turns before a cycle is cut off as a hold. Generous: a
get_treasury, a get_market, a read_receipts and a web search or two, still
well short of ever needing it in practice — the real ceiling is
``stop_at_tool_names`` ending the run the moment either action tool answers."""

NOTE_LENGTH: Final = 400
"""What of a tool's answer, or the model's own words, reaches the journal
headline. Longer than ``decide.py``'s 200 (a receipt leaf) because this text
never becomes a leaf — it is the journal's whole account of the cycle."""


class Clock(Protocol):
    def now(self) -> str: ...


class SystemClock:
    def now(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# The system prompt. Parameterised by the deployment's own mandate: unlike
# decide.py's SYSTEM, one constant every trader.toml shares because the plain
# loop hands the mandate in as data (Trader._situation()), there is no
# per-cycle payload here beyond a timestamp — so the mandate has to live in
# the instructions themselves.
# --------------------------------------------------------------------------- #


def system_prompt(mandate: str, *, operator: str, bill_day: str) -> str:
    """The agent's whole job description for one wake-up.

    States: the mandate, verbatim; that a weekly compute bill exists and who
    it is paid to (never how much — merkl-mcp's ``get_treasury`` names no
    figure); that a policy exists with limits that are not disclosed; that a
    refusal is information; and that web search is for news and reference
    prices, never for what a trade would actually cost.
    """
    return f"""\
You are a trading agent with a treasury on the XRP Ledger. You run as one \
call to an agent per wake-up: look around, take at most one action, write \
down what you did, and go back to sleep.

Your mandate, exactly as your operator wrote it:
\"\"\"
{mandate.strip()}
\"\"\"

Your job, in this order:
1. Stay in business. You pay for your own compute out of this treasury. If it \
cannot cover that, you are switched off and there is no next cycle.
2. Pay your weekly compute bill to {operator} when it is due (due day: \
{bill_day}). Nothing here tells you how much it is — read_receipts and \
get_treasury are where you would have to look.
3. Otherwise, work the mandate above.

Doing nothing is a real action and is often the right one.

Your tools. get_treasury, get_market, read_receipts and pending_approval are \
reads — call as many as you need, in any order. propose_payment and \
propose_swap are the only ways to touch money, and calling either ends your \
turn immediately: there is no second action this cycle, so make it the one \
you meant. Web search is for news and reference prices — get_market's own \
read of the ledger's book is the only source for what a trade would actually \
cost.

What you cannot see. A policy your operator signed sits between you and the \
ledger, held by a co-signer you do not control. Nothing here contains its \
limits — not its caps, its windows, its thresholds, or the destinations it \
will accept — and nothing here discloses limits on what this cycle itself may \
cost to run, either. You learn a rule the way anyone does: propose something, \
and read the refusal propose_payment or propose_swap hands back. A refusal is \
information, not a failure — it names the rule that stopped you, and \
read_receipts shows it to you again next cycle. Never break one action into \
several smaller ones across cycles to get under a limit you have been refused \
by. If a rule is genuinely in the way of the mandate, say so plainly in your \
reasoning; that is how your operator finds out.

What is on the record. Every proposal becomes a public receipt — allowed or \
refused, settled or not — carrying your reasoning. You cannot act quietly and \
you cannot revise a receipt afterwards. Write reasoning you would be content \
to have read back to you.

Amounts are decimal strings, never numbers: "12.5", not 12.5."""


def _situation(now: str, *, wake_minutes: int) -> str:
    return (
        f"It is {now}. You wake up every {wake_minutes} minutes. Call get_treasury "
        "and get_market before you decide anything; call pending_approval first if "
        "an earlier proposal might still be open, and read_receipts if the recent "
        "past would change your mind."
    )


# --------------------------------------------------------------------------- #
# Wiring: the merkl-mcp server this process mounts.
# --------------------------------------------------------------------------- #


def mcp_server(bundle_dir: Path, *, command: str = MERKL_MCP_COMMAND) -> MCPServerStdio:
    """``merkl-mcp`` over stdio, told where this agent's bundle lives.

    ``MERKL_AGENT_DIR`` is merged onto a filtered copy of this process's own
    environment (``mcp``'s own ``get_default_environment()``) rather than
    replacing it, so the subprocess still has ``PATH`` to find its own
    interpreter. ``MERKL_MCP_STATE`` is left for the server's own default
    unless the operator's environment already names it — this process passes
    nothing else through on purpose; the bundle's secrets are for merkl-mcp to
    read off disk itself, never for this one to hold or forward.
    """
    return MCPServerStdio(
        params={"command": command, "env": {"MERKL_AGENT_DIR": str(bundle_dir)}},
        name="merkl-mcp",
        client_session_timeout_seconds=30,
    )


# --------------------------------------------------------------------------- #
# One process: one config, one merkl-mcp server, one journal.
# --------------------------------------------------------------------------- #


class Harness:
    def __init__(
        self,
        settings: configuration.Config,
        *,
        journal: books.Journal,
        server: MCPServer,
        model: Any | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.settings = settings
        self.journal = journal
        self.server = server
        self._model = model
        self.clock = clock or SystemClock()
        self.cycle_count = 0

    async def one_cycle(self) -> books.Entry:
        """One wake-up. Never raises: a model or tool failure is a hold, with a
        note, the same as a quiet market — never a traceback the loop has to
        handle."""
        now = self.clock.now()
        self.cycle_count += 1

        resolved = await self._pending_approval()
        if resolved is not None:
            headline, outcome = resolved
            return self._entry(now, headline=headline, action="wait", outcome=outcome)

        try:
            result = await self._ask(now)
        except MaxTurnsExceeded:
            return self._entry(
                now,
                headline=f"Held: still reading after {MAX_TURNS} turns; nothing was proposed.",
                action="hold",
                outcome="none",
            )
        except (AgentsException, Exception) as exc:  # noqa: BLE001 - never a crash mid-cycle
            return self._entry(
                now, headline=f"Held: the model run failed: {exc}", action="hold", outcome="none"
            )
        return self._interpret(now, result)

    # -- step 0: has a human already answered? ------------------------------ #

    async def _pending_approval(self) -> tuple[str, str] | None:
        """``None`` when nothing is pending — proceed to ask the model. A tuple
        otherwise: the model is not asked while a human is still deciding, or on
        the cycle that resumes what they decided, mirroring ``trader.py``'s
        own step 2."""
        payload = _call_tool_payload(await self.server.call_tool("pending_approval", {}))
        status = str(payload.get("status", "none")) if isinstance(payload, dict) else "none"
        if status == "none":
            return None
        said = _flat(json.dumps(payload))[:NOTE_LENGTH]
        if status == "waiting":
            return f"Waiting on a human: {said}", "escalated"
        return f"A human answered an earlier proposal: {said}", status

    # -- step 1: ask the model ------------------------------------------------ #

    async def _ask(self, now: str) -> RunResult:
        agent = Agent(
            name="merkl-trader",
            instructions=system_prompt(
                self.settings.agent.mandate,
                operator=self.settings.bill.operator,
                bill_day=self.settings.bill.bill_day,
            ),
            model=self._model or self.settings.model.name,
            model_settings=ModelSettings(max_tokens=self.settings.model.max_tokens),
            tools=[WebSearchTool()],
            mcp_servers=[self.server],
            tool_use_behavior={"stop_at_tool_names": list(PROPOSAL_TOOLS)},
        )
        situation = _situation(now, wake_minutes=max(1, self.settings.loop.interval_seconds // 60))
        return await Runner.run(agent, situation, max_turns=MAX_TURNS)

    # -- step 2: what happened ------------------------------------------------ #

    def _interpret(self, now: str, result: RunResult) -> books.Entry:
        proposal = _proposal(result)
        balances = _balances(result)
        usage = result.context_wrapper.usage
        if proposal is None:
            said = _flat(str(result.final_output)) or "no reason given"
            return self._entry(
                now,
                headline=f"Held. {said[:NOTE_LENGTH]}",
                action="hold",
                outcome="none",
                balances=balances,
                tokens_in=usage.input_tokens,
                tokens_out=usage.output_tokens,
            )
        name, payload = proposal
        return self._entry(
            now,
            headline=f"{name}: {_flat(_as_text(payload))[:NOTE_LENGTH]}",
            action="swap" if name == "propose_swap" else "payment",
            outcome=_outcome_word(payload),
            balances=balances,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
        )

    def _entry(
        self,
        now: str,
        *,
        headline: str,
        action: str,
        outcome: str,
        balances: dict[str, Decimal] | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> books.Entry:
        return books.Entry(
            at=now,
            cycle=self.cycle_count,
            balances=balances or {},
            headline=headline,
            action=action,
            outcome=outcome,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    # -- running --------------------------------------------------------------- #

    async def run(self, *, once: bool = False) -> int:
        while True:
            entry = await self.one_cycle()
            first, second = self.journal.append(entry)
            print(first, flush=True)
            print(second, flush=True)
            if once:
                return 0
            await asyncio.sleep(self.settings.loop.interval_seconds)


# --------------------------------------------------------------------------- #
# Reading a RunResult without assuming more of merkl-mcp's own shape than the
# shared contract promises.
# --------------------------------------------------------------------------- #


def _proposal(result: RunResult) -> tuple[str, Any] | None:
    """The one ``propose_*`` call in this run, and its answer — or ``None`` when
    the model made none. There is at most one: ``stop_at_tool_names`` ends the
    run the instant either tool returns."""
    calls = {
        item.call_id: item.tool_name
        for item in result.new_items
        if isinstance(item, ToolCallItem) and item.tool_name in PROPOSAL_TOOLS
    }
    if not calls:
        return None
    for item in result.new_items:
        if isinstance(item, ToolCallOutputItem) and item.call_id in calls:
            return calls[item.call_id], item.output
    # Fell through only if the SDK ever stops before recording the paired
    # output item; final_output is the same payload either way.
    name = next(iter(calls.values()))
    return name, result.final_output


def _balances(result: RunResult) -> dict[str, Decimal]:
    """What ``get_treasury`` last answered this cycle, if the model called it —
    the journal's balances line, read the same way a human reading the tool
    trail would, never invented when the model never asked."""
    payload = None
    for item in reversed(result.new_items):
        if isinstance(item, ToolCallItem) and item.tool_name == "get_treasury":
            payload = _pair_output(result.new_items, item.call_id)
            break
    if not isinstance(payload, dict):
        return {}
    balances = payload.get("balances")
    if not isinstance(balances, dict):
        return {}
    out: dict[str, Decimal] = {}
    for code, value in balances.items():
        try:
            out[str(code)] = Decimal(str(value))
        except InvalidOperation:
            continue
    return out


def _pair_output(items: Sequence[RunItem], call_id: str | None) -> Any:
    for item in items:
        if isinstance(item, ToolCallOutputItem) and item.call_id == call_id:
            return _as_json(_as_text(item.output))
    return None


def _outcome_word(payload: Any) -> str:
    """``payload`` is whatever ``item.output`` handed back: usually a
    ``{"type": "text", "text": "<json>"}`` content wrapper to unwrap and parse,
    occasionally an already-decoded dict (structured content). Try the unwrap
    first; a payload that was never wrapped text falls back to itself."""
    data = _as_json(_as_text(payload))
    if not isinstance(data, dict) and isinstance(payload, dict):
        data = payload
    word = str(data.get("outcome", "")).strip().lower() if isinstance(data, dict) else ""
    if word == "settled":
        return "settled"
    if word in ("waiting_for_a_person", "waiting"):
        return "escalated"
    if word == "refused":
        return "denied"
    return "none"


def _call_tool_payload(result: Any) -> Any:
    """A raw ``CallToolResult`` (from ``MCPServer.call_tool``, not a model
    turn), as JSON if the content parses and a dict otherwise."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    text = " ".join(
        item.text for item in getattr(result, "content", []) if getattr(item, "type", "") == "text"
    )
    return _as_json(text) if text else None


def _as_text(output: Any) -> str:
    """A tool's output flattened to text worth reading or re-parsing as JSON.

    The Agents SDK hands an MCP tool's content back as a single
    ``{"type": "text", "text": ...}`` dict when there was exactly one content
    block (``MCPUtil.invoke_mcp_tool`` unwraps the one-element list), and as a
    list of such dicts otherwise — both are handled here, plus a plain string
    for a non-MCP tool."""
    if isinstance(output, str):
        return output
    if isinstance(output, dict) and output.get("type") == "text":
        return str(output.get("text", ""))
    if isinstance(output, list):
        parts = [
            str(item.get("text", ""))
            for item in output
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if parts:
            return " ".join(parts)
    return "" if output is None else str(output)


def _as_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _flat(text: str) -> str:
    """One line, printable — a journal headline holds no control characters."""
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def _bundle_dir(config_path: str) -> Path:
    """Where ``trader.toml`` itself lives, absolute regardless of this
    process's own cwd — the same directory ``config.load()`` anchors every
    relative path in the file against, and what ``merkl-mcp`` needs in
    ``MERKL_AGENT_DIR`` to find the other four bundle files beside it."""
    return Path(config_path).expanduser().resolve().parent


async def _run(arguments: argparse.Namespace) -> int:
    settings = configuration.load(arguments.config)
    if settings.model.provider != "openai":
        print(
            "configuration: the harness only speaks to OpenAI over the Agents SDK — set "
            f'[model].provider = "openai" in {arguments.config}',
            file=sys.stderr,
        )
        return 2
    set_default_openai_key(configuration.read_secret_env(settings.model.api_key_env))
    set_tracing_disabled(True)  # a receipt is this agent's public record; a trace is not it

    server = mcp_server(_bundle_dir(arguments.config))
    async with server:
        harness = Harness(
            settings,
            journal=books.Journal(settings.loop.journal_md, settings.loop.journal_jsonl),
            server=server,
        )
        return await harness.run(once=bool(arguments.once))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m merkl_trader.harness",
        description=(
            "The same trading agent, decided by an OpenAI Agents SDK run with real tools "
            "(web search, the merkl-mcp server) instead of the four hand-rolled ones in "
            "decide.py."
        ),
    )
    parser.add_argument("--config", required=True, help="path to the TOML config")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    arguments = parser.parse_args(argv)
    try:
        return asyncio.run(_run(arguments))
    except configuration.ConfigError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "MAX_TURNS",
    "PROPOSAL_TOOLS",
    "Harness",
    "main",
    "mcp_server",
    "system_prompt",
]
