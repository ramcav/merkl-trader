# Changelog

All notable changes to `merkl-trader`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are cut by pushing a `v<version>` tag; see
`.github/workflows/release.yml`.

## [Unreleased]

- **Each harness cycle is a sealed Merkl session** with every tool call recorded as an action
  and the receipt joined through `merkl-mcp`'s new `session_id` argument (`harness/sessions.py`).

- The harness prompt explains that XRP amounts in book offers are drops (with the
  price arithmetic) and points the bill paragraph at `get_treasury`'s `compute_bill`.
  The harness now writes `cost_usd` per cycle and passes `MERKL_TRADER_HOME` to merkl-mcp,
  which computes the bill from that journal.

## [0.2.0] - 2026-09-16

### Added

- **A second reference agent, `python -m merkl_trader.harness`.** The same
  mandate, journal and wake-up interval as `trader.py`, decided by one run of
  an OpenAI Agents SDK agent (`openai-agents`) instead of `decide.py`'s four
  hand-rolled tools: `WebSearchTool` for news and reference prices, and the
  [`merkl-mcp`](https://github.com/ramcav/merkl-mcp) server mounted over
  stdio for the treasury, the receipts and the one proposal at a
  time rule. `Agent.tool_use_behavior={"stop_at_tool_names": (...)}` ends a
  turn structurally the instant `propose_payment` or `propose_swap` answers.
  Tested end to end against a fake `merkl-mcp` (`tests/fake_mcp_server.py`,
  a real stdio subprocess) and the Agents SDK's own `ScriptedModel` test
  double — no key, no network.

- **The harness takes its market from existing MCP servers.** Beside
  `merkl-mcp` (money and evidence only: `get_treasury`, `propose_payment`,
  `propose_swap`, `pending_approval`, `read_receipts`, `verify_receipt`) and
  `WebSearchTool`, it mounts lgcarrier's `xrpl-mcp-server` over stdio
  (`XRPL_NODE_URL` from `[rail].json_rpc_url`), tool-filtered to
  `get_book_offers`, `get_account_info`, `get_account_lines` and
  `get_transaction_info` so `submit_transaction` is never visible, and
  CoinGecko's keyless remote MCP (`https://mcp.api.coingecko.com/mcp`) for
  reference prices, switchable with `[harness] coingecko = false`. The system
  prompt names the sources. The ledger server runs as `python -m xrpl_mcp_server` (its console script is
  broken) from its own `mcp<2` venv in the Dockerfile (`XRPL_MCP_PYTHON`); a
  server that fails to start is named at the head of every journal line.

### Fixed

- **A refused or unaffordable compute bill never ends the process.** It is journaled
  ("bill refused: <rule>; retrying on <time>"), stays owed and is retried at most
  once an hour; the same intent (destination, amount within 1%) is never refused-and-filed
  twice inside an hour, across restarts; "out of business" idles in place and says so
  hourly instead of exiting, so a restart policy cannot turn it into a flood of receipts.

- **A placeholder `[bill].operator` is refused at load.** `config.load()` now
  checks it is a valid classic XRPL address and names the field, instead of
  failing when the bill comes due.
- **A proposal that raises is a journaled hold, never an exit.** Both loops
  write "could not propose: <reason>" and carry on; `trader.py`'s `run()` also
  survives any other cycle exception. The compute bill keeps one receipt id
  (derived from the last-paid date) and stays in flight until a receipt exists,
  so a restart no longer mints a new id or writes a new "restarted" note.

- **An unreadable bundle file now names the fix.** The image runs as uid
  10002 and every secret `merkl treasury init` writes is 0600, so a
  bind-mounted bundle owned by a different uid was the single most common
  first-deploy failure — and a bare `PermissionError` gave no hint what to
  do about it. `config.permission_error()` turns that into a one-line
  `chown -R 10002:10002 <dir>`; `config.load()`, `read_secret_file()` and the
  new `read_bundle_bytes()` (the agent's Ed25519 key) all route through it,
  and `trader.py`'s `build()` wraps `load_wallets()` the same way. A missing
  file, as opposed to an unreadable one, keeps its plain message.

## [0.1.1] - 2026-09-16

### Fixed

- **A refused or unaffordable compute bill never ends the process.** It is journaled
  ("bill refused: <rule>; retrying on <time>"), stays owed and is retried at most
  once an hour; the same intent (destination, amount within 1%) is never refused-and-filed
  twice inside an hour, across restarts; "out of business" idles in place and says so
  hourly instead of exiting, so a restart policy cannot turn it into a flood of receipts.

- **Relative paths in the bundle now resolve against the config file, not
  the process's working directory.** `key_file`, `wallet_file`,
  `token_file`, `api_key_file` and `[loop].home` are relative by design, so
  the bundle folder can be moved or bind-mounted — but `config.py` was
  resolving them against the current working directory, which crashed the
  container (`--config /agent/trader.toml`, cwd elsewhere) with
  `FileNotFoundError: 'agent-ed25519.pem'`. `load()` now anchors every
  relative path to `trader.toml`'s own directory; `~` still expands, and an
  already-absolute path is left alone.

## [0.1.0] - 2026-09-14

### Added

- The reference trading agent, split out of `merkl-sdk`'s `examples/trader`
  into its own repository and package, `merkl_trader`, run as
  `python -m merkl_trader`.
- An OpenAI provider for `decide()` beside the Anthropic one
  (`[model].provider`, default `"anthropic"`).
