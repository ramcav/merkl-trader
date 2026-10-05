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
tests/test_trader.py    end to end against merkl-sdk's in-memory rail, a real
                         signer and a scripted model
tests/test_config.py    the $MERKL_TRADER_HOME override and path resolution
                         (relative, absolute, ~)
config.example.toml     a worked example, commented — copy to trader.toml
Dockerfile              python -m merkl_trader, read-only /agent
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

## Guidelines

- This repo imports `merkl.*` from the installed `merkl-sdk` package only —
  never a relative path into a merkl-sdk checkout. A change to the SDK reaches
  here through a release, not a shared filesystem.
- No TOML float for a money number, ever — amounts and prices are decimal
  strings, checked at parse.
- `config.py` has no section for the policy's rules and never will: the agent
  learns them from refusals, not from its own config file.
