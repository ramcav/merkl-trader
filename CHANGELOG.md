# Changelog

All notable changes to `merkl-trader`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are cut by pushing a `v<version>` tag; see
`.github/workflows/release.yml`.

## [Unreleased]

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
