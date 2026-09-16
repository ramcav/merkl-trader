# Changelog

All notable changes to `merkl-trader`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are cut by pushing a `v<version>` tag; see
`.github/workflows/release.yml`.

## [Unreleased]

## [0.2.0] - 2026-09-16

### Added

- **A second reference agent, `python -m merkl_trader.harness`.** The same
  mandate, journal and wake-up interval as `trader.py`, decided by one run of
  an OpenAI Agents SDK agent (`openai-agents`) instead of `decide.py`'s four
  hand-rolled tools: `WebSearchTool` for news and reference prices, and the
  [`merkl-mcp`](https://github.com/ramcav/merkl-mcp) server mounted over
  stdio for the treasury, the market, the receipts and the one proposal at a
  time rule. `Agent.tool_use_behavior={"stop_at_tool_names": (...)}` ends a
  turn structurally the instant `propose_payment` or `propose_swap` answers.
  Tested end to end against a fake `merkl-mcp` (`tests/fake_mcp_server.py`,
  a real stdio subprocess) and the Agents SDK's own `ScriptedModel` test
  double — no key, no network.

### Fixed

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
