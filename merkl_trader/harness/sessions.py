"""One Merkl session per cycle: every tool call an action, sealed at the end.

``Harness`` opens ``client.session(...)`` around a run; :class:`CycleRecorder`
(``RunHooks``) records each tool call as an action with its name, input and
output (output cut to 4 KB), and :class:`SessionedServer` hands the session id
to ``merkl-mcp``'s ``propose_*`` tools on the wire, so the receipt joins the
session. The model never sees or supplies those arguments.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, Final

from agents import RunHooks
from agents.mcp import MCPServerStdio

OUTPUT_LIMIT: Final = 4096
PROPOSAL_TOOLS: Final = ("propose_payment", "propose_swap")
HIDDEN_ARGUMENTS: Final = ("session_id", "session_action_count", "depends_on")
"""Filled in by :class:`SessionedServer`; stripped from the schemas the model reads."""


def clip(value: Any, limit: int = OUTPUT_LIMIT) -> Any:
    """Text output cut to ``limit`` characters; anything else flattened to text first."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def goal_of(mandate: str) -> str:
    """The mandate's first sentence."""
    text = " ".join(mandate.split())
    for end in (". ", "! ", "? "):
        if end in text:
            return text.split(end)[0] + end.strip()
    return text


class CycleRecorder(RunHooks):
    """Records the run's tool calls into one session, best effort: a notary that
    is down loses the action, never the cycle."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.last_action_id: str | None = None
        self._started: dict[str, float] = {}
        self._lock = asyncio.Lock()
        """One recording at a time: each action is posted after the previous one
        landed, so each gets the next leaf index, in the order the calls finished."""
        self._pending = 0
        self._quiet = asyncio.Condition()

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        self._started[_call_id(context)] = time.monotonic()
        if _tool_name(context, tool) not in PROPOSAL_TOOLS:
            self._pending += 1

    async def settled(self) -> None:
        """Wait until every recorded tool call has been posted (so the session's
        ``action_count`` is exact before a proposal is sent)."""
        async with self._quiet:
            await self._quiet.wait_for(lambda: self._pending == 0)

    async def _finished(self) -> None:
        async with self._quiet:
            self._pending -= 1
            self._quiet.notify_all()

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        name = _tool_name(context, tool)
        output = clip(result)
        counted = name not in PROPOSAL_TOOLS
        try:
            if name in PROPOSAL_TOOLS and '"error"' not in output:
                return  # the receipt is this call's action; the builder records it
            started = self._started.pop(_call_id(context), time.monotonic())
            await self.record(
                name,
                _arguments(context),
                output,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if counted:
                await self._finished()

    async def record(
        self, tool_name: str, input_data: Any, output_data: Any, *, duration_ms: int = 0
    ) -> None:
        async with self._lock:
            try:
                recorded = await self.session.record_action(
                    tool_name=tool_name,
                    input_data=input_data,
                    output_data=output_data,
                    duration_ms=duration_ms,
                    depends_on=[self.last_action_id] if self.last_action_id else [],
                )
            except Exception as exc:  # noqa: BLE001 - recording never ends a cycle
                print(f"harness: could not record {tool_name}: {exc}", file=sys.stderr)
                return
            if isinstance(recorded, dict) and recorded.get("action_id"):
                self.last_action_id = str(recorded["action_id"])


def _tool_name(context: Any, tool: Any) -> str:
    return str(getattr(context, "tool_name", "") or getattr(tool, "name", "tool"))


def _call_id(context: Any) -> str:
    return str(getattr(context, "tool_call_id", "") or id(context))


def _arguments(context: Any) -> Any:
    raw = getattr(context, "tool_arguments", "") or ""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


class SessionedServer(MCPServerStdio):
    """``merkl-mcp`` over stdio that tells ``propose_*`` which session it is in."""

    recorder: CycleRecorder | None = None

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> Any:
        recorder = self.recorder
        if recorder is not None and tool_name in PROPOSAL_TOOLS and recorder.session.session_id:
            await recorder.settled()
            arguments = {
                **(arguments or {}),
                "session_id": recorder.session.session_id,
                "session_action_count": recorder.session.action_count,
            }
            if recorder.last_action_id:
                arguments["depends_on"] = recorder.last_action_id
        return await super().call_tool(tool_name, arguments, meta)

    async def list_tools(self, run_context: Any = None, agent: Any = None) -> list[Any]:
        tools = await super().list_tools(run_context, agent)
        return [_without_hidden(tool) for tool in tools]


def _without_hidden(tool: Any) -> Any:
    schema = getattr(tool, "input_schema", None)
    if tool.name not in PROPOSAL_TOOLS or not isinstance(schema, dict):
        return tool
    properties = {
        k: v for k, v in (schema.get("properties") or {}).items() if k not in HIDDEN_ARGUMENTS
    }
    return tool.model_copy(update={"input_schema": {**schema, "properties": properties}})
