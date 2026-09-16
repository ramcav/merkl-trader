# merkl-trader

The reference Merkl trading agent. Standalone repository (split out of
`ramcav/merkl-sdk` in phase 20, 2026-09-14), built on the *released*
`merkl-sdk` — a change to the SDK reaches this repo only through a version
bump and release, same relationship merkl-api has with the SDK.

## What this is

A small process that wakes up on an interval, reads the XRP/RLUSD book on the
XRP Ledger, decides at most one thing, and writes down what it did. It trades
through a Merkl policy it cannot read — it learns what it may do from the
signer's refusals, in words, on the next cycle — pays its own compute bill out
of the treasury it trades, and every decision it makes becomes a public
receipt. See `README.md` for the full story.

```
merkl_trader/
  trader.py             the loop and the wiring
  market.py             what the ledger says
  decide.py             what the model says — the part you replace
  ledger.py             the agent's own books: state, the compute bill, journal
  config.py             one TOML file, validated once; every relative path in
                         it resolves against trader.toml's own directory, not
                         the process's cwd; $MERKL_TRADER_HOME overrides
                         [loop].home for the Docker image
harness/loop.py         python -m merkl_trader.harness: the same mandate,
                         journal and interval, decided by one OpenAI Agents
                         SDK run against WebSearchTool and the merkl-mcp
                         server (stdio) instead of decide.py's four tools
tests/test_trader.py    end to end against merkl-sdk's in-memory rail, a real
                         signer and a scripted model
tests/test_harness.py   end to end against a fake merkl-mcp (stdio) and the
                         Agents SDK's own ScriptedModel test double
tests/test_config.py    the $MERKL_TRADER_HOME override, path resolution
                         (relative, absolute, ~) and the chown-10002 hint on
                         an unreadable bundle file
config.example.toml     a worked example, commented — copy to trader.toml
Dockerfile              python -m merkl_trader (default) or
                         python -m merkl_trader.harness, read-only /agent
```

## Install

```bash
uv venv .venv
uv pip install -p .venv/bin/python -e ".[dev]"
```

`pyproject.toml` pins `merkl-sdk[xrpl]>=0.3.0` with a comment to raise it to
`>=0.3.1` once that release exists on PyPI — 0.3.1 is what phase 20 actually
needs (the enrol answer's public urls, the managed signer's secret scrubbing),
but 0.3.0 is what installs today.

## Testing

```bash
pytest -q
ruff check merkl_trader tests
ruff format --check merkl_trader tests
```

`tests/test_trader.py`: everything below the model is real — a real
`SignerEngine` with a real encrypted keystore, real receipts on disk, against
`merkl.adapters.fake`'s in-memory rail. Only the model (scripted replies) and
the XRPL node (`httpx.MockTransport`) are stubbed.

## The image

`Dockerfile` builds `ghcr.io/ramcav/merkl-trader`. Non-root from the start,
read-only `/agent`, journal and state on `/var/lib/merkl-trader`
(`$MERKL_TRADER_HOME`). `.github/workflows/release.yml` builds and pushes it
on every `v*` tag; `ci.yml` runs tests and ruff on every push and PR.

The box that runs merkl-api also runs this, under a compose profile that a
plain `docker compose up -d` never starts — see merkl-api's
`docker-compose.prod.yml` and its README for `--profile trader`.

## The model provider

`[model].provider` (`config.py`, `MODEL_PROVIDERS`) is `"anthropic"` (the
default, so an older config is unchanged) or `"openai"`. `decide.py` keeps
the Anthropic loop exactly as it was and adds a second one, `_decide_openai`,
behind the same four `TOOLS`, converted to OpenAI's function-calling shape
(`OPENAI_TOOLS`) rather than duplicated. `trader.py`'s `_model_client()`
picks `AnthropicModel` or `OpenAIModel` from the same setting; `decide()`
branches on the `provider` string, not on that object's type, so a test can
hand either loop a plain `ModelPort` fake. An OpenAI tool call whose
arguments are not valid JSON, or a model call that raises (bad key, no
network), ends the cycle as a hold with a note — never a proposal built from
blanks, never a traceback the caller has to handle.

## The harness (`harness/loop.py`)

`python -m merkl_trader.harness --config trader.toml [--once]` — phase 23's
second reference agent, built on `openai-agents` instead of a hand-rolled
tool loop. It requires `[model].provider = "openai"` in the same
`trader.toml` (a plain `ConfigError` otherwise) and mounts two tool sources on
an `agents.Agent`: `WebSearchTool()` and `merkl-mcp` over stdio
(`MCPServerStdio`, command `merkl-mcp`, `MERKL_AGENT_DIR` set to the config's
own directory). It holds none of `trader.py`'s state — no nonce, no in-flight
bookkeeping, no `Bill` accrual — because the market, the treasury, the
receipts and the one-proposal-at-a-time rule all live behind merkl-mcp's own
`ReceiptBuilder` now; this process only journals what came back.
`Agent.tool_use_behavior={"stop_at_tool_names": ("propose_payment",
"propose_swap")}` ends a turn structurally the instant either tool answers,
the SDK-native equivalent of `decide.py` stopping at the first `tool_use`
block. `tests/fake_mcp_server.py` (`mcp.server.mcpserver.MCPServer`, the
`mcp>=2` API — merkl-mcp itself pins `mcp<2` for `FastMCP`, a different
package the wire protocol doesn't care about) stands in for merkl-mcp in
tests, scripted by one JSON file (`FAKE_MCP_SCRIPT`); `agents.testing
.ScriptedModel` stands in for the model, the Agents SDK's own test double.

## Unreadable bundle files

The image runs as uid 10002 and every bundle secret is 0600
(`config.example.toml`), so a bind-mounted bundle owned by a different uid is
the first thing a fresh deploy gets wrong. `config.permission_error()` turns
that `PermissionError` into a one-line fix naming `chown -R 10002:10002
<dir>`; `config.load()`, `read_secret_file()` and the new
`read_bundle_bytes()` (the Ed25519 key, read as bytes) all route through it,
and `trader.py`'s `build()` wraps `load_wallets()` the same way. A missing
file (as opposed to an unreadable one) keeps the plain message — there is
nothing to `chown`.

## Guidelines

- This repo imports `merkl.*` from the installed `merkl-sdk` package only —
  never a relative path into a merkl-sdk checkout. A change to the SDK reaches
  here through a release, not a shared filesystem.
- No TOML float for a money number, ever — amounts and prices are decimal
  strings, checked at parse.
- `config.py` has no section for the policy's rules and never will: the agent
  learns them from refusals, not from its own config file.
