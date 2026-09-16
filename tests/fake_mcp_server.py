"""A fake ``merkl-mcp``, speaking Phase 23's shared contract over stdio.

merkl-mcp is a sibling repository another engineer builds in parallel
(``/Users/ricardomendezcavalieri/Developer/projects/merkl/merkl-mcp``); this
file is not it and never imports from it. It exists so ``test_harness.py`` can
run the real ``merkl_trader.harness`` code — a real ``MCPServerStdio``
subprocess, a real stdio handshake — against something that answers the six
tools the brief's contract table names, without depending on the real
package's own progress.

Driven entirely by one JSON file named by ``FAKE_MCP_SCRIPT`` (read once, at
startup):

    {
      "treasury": {...},           # get_treasury's answer
      "market": {...},             # get_market's answer, whatever the input
      "receipts": [...],           # read_receipts's answer
      "propose": {...},            # what propose_payment/propose_swap answers
      "pending_sequence": [...]    # pending_approval's answers, one per call;
                                    # the last one repeats once exhausted
    }

Every key is optional; a script that omits one gets a bland default. The one
piece of contract behaviour this fakes rather than just echoes: **one
proposal at a time**. If a scripted ``propose`` would put a proposal in front
of a person (``outcome: "waiting_for_a_person"``), this process remembers
that, and every ``propose_*`` call after it returns ``waiting_for_a_person``
again — never the script's answer — until a test overwrites the script or
restarts the server, exactly as the brief promises: "a second propose_* while
one is waiting returns waiting_for_a_person without proposing."
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

_DEFAULT_TREASURY: dict[str, Any] = {
    "address": "rTREASURYexampleaccount0000000000",
    "network": "xrpl",
    "balances": {"XRP": "500", "RLUSD": "40"},
    "policy_version": "2026.09.16",
    "signer_health": "ok",
    "pending_with_a_person": False,
}

_DEFAULT_MARKET: dict[str, Any] = {
    "pair": "XRP/RLUSD",
    "best_bid": "0.520000",
    "best_ask": "0.530000",
    "mid": "0.525000",
    "sizes": {},
}

_DEFAULT_PROPOSE: dict[str, Any] = {"outcome": "refused", "rule": "no script configured a propose answer"}


def _load_script() -> dict[str, Any]:
    path = os.environ.get("FAKE_MCP_SCRIPT")
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - a broken test fixture
        print(f"fake-mcp-server: cannot read FAKE_MCP_SCRIPT={path}: {exc}", file=sys.stderr)
        return {}


_SCRIPT = _load_script()
_PENDING_SEQUENCE = list(_SCRIPT.get("pending_sequence") or [{"status": "none"}])
_PENDING_INDEX = 0
_HELD: dict[str, Any] | None = None
"""Set once a propose_* answers waiting_for_a_person; cleared only by
restarting this process — there is no resume path here, only the one-at-a-time
refusal. A real merkl-mcp resumes it; that behaviour is that repository's to
test, not this fake's."""

server = MCPServer(name="fake-merkl-mcp")


@server.tool()
def get_treasury() -> dict[str, Any]:
    return dict(_SCRIPT.get("treasury") or _DEFAULT_TREASURY)


@server.tool()
def get_market(
    base: str = "XRP",
    quote_code: str = "RLUSD",
    quote_issuer: str = "",
    sizes: list[str] | None = None,
) -> dict[str, Any]:
    return dict(_SCRIPT.get("market") or _DEFAULT_MARKET)


@server.tool()
def read_receipts(limit: int = 5) -> list[Any]:
    receipts = list(_SCRIPT.get("receipts") or [])
    return receipts[-max(1, limit) :]


@server.tool()
def propose_payment(
    destination: str,
    amount: str,
    currency: str,
    why: str,
    issuer: str | None = None,
) -> dict[str, Any]:
    return _propose()


@server.tool()
def propose_swap(
    sell_amount: str,
    sell_currency: str,
    buy_amount: str,
    buy_currency: str,
    why: str,
    sell_issuer: str | None = None,
    buy_issuer: str | None = None,
) -> dict[str, Any]:
    return _propose()


@server.tool()
def pending_approval() -> dict[str, Any]:
    global _PENDING_INDEX
    sequence = _PENDING_SEQUENCE
    index = min(_PENDING_INDEX, len(sequence) - 1)
    _PENDING_INDEX += 1
    answer = dict(sequence[index])
    if answer.get("status") not in ("none", "waiting"):
        # A resolution was just handed back; the one-at-a-time hold is over.
        global _HELD
        _HELD = None
    return answer


def _propose() -> dict[str, Any]:
    global _HELD
    if _HELD is not None:
        return dict(_HELD)
    answer = dict(_SCRIPT.get("propose") or _DEFAULT_PROPOSE)
    if answer.get("outcome") == "waiting_for_a_person":
        _HELD = answer
    return answer


if __name__ == "__main__":
    server.run(transport="stdio")
