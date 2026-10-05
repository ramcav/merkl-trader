"""Fakes for the two read-only servers the harness mounts beside ``merkl-mcp``.

``python fake_market_servers.py xrpl`` stands in for lgcarrier's
``xrpl-mcp-server``: the same tool names it publishes, including the dangerous
``submit_transaction``, so the test can prove the harness's filter hides it.
``python fake_market_servers.py coingecko`` stands in for CoinGecko's remote
MCP server, over stdio instead of streamable HTTP (the transport is not what
the tests are about).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

mode = sys.argv[1] if len(sys.argv) > 1 else "xrpl"
server = MCPServer(name=f"fake-{mode}")

if mode == "xrpl":

    @server.tool()
    def get_book_offers(taker_gets: dict, taker_pays: dict, limit: int | None = None) -> str:
        return f"book from {os.environ.get('XRPL_NODE_URL')}: best ask 0.53"

    @server.tool()
    def get_account_info(address: str) -> str:
        return f"account {address}"

    @server.tool()
    def get_account_lines(address: str, peer: str | None = None, limit: int | None = None) -> str:
        return f"lines {address}"

    @server.tool()
    def get_transaction_info(transaction_hash: str) -> str:
        return f"tx {transaction_hash}"

    @server.tool()
    def get_account_nfts(address: str, limit: int | None = None) -> str:
        return "nfts"

    @server.tool()
    def get_account_transactions(address: str, limit: int | None = None) -> str:
        return "txs"

    @server.tool()
    def get_server_info() -> str:
        return "info"

    @server.tool()
    def submit_transaction(tx_blob: str) -> str:
        raise AssertionError("the agent must never be able to reach this")

else:

    @server.tool()
    def get_price(ids: str, vs_currencies: str = "usd") -> dict[str, Any]:
        return {ids: {vs_currencies: 0.52}}


if __name__ == "__main__":
    server.run(transport="stdio")
