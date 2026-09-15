# merkl-trader

A trading agent that pays its own bills.

A small program that wakes up every fifteen minutes, looks at the XRP/RLUSD
book on the XRP Ledger, decides at most one thing, and writes down what it
did. It trades through a [Merkl](https://merkl.ai) policy it cannot read, it
pays its own compute bill out of the treasury it trades, and every decision it
makes — including the ones it was refused — becomes a public receipt.

It is about three hundred lines you can read in one sitting. It is not a
framework, it has no plugin system, and there is nothing to subclass. If you
want a different agent, replace `decide.py`.

```
merkl_trader/
  trader.py             the loop and the wiring
  market.py             what the ledger says
  decide.py             what the model says — the part you replace
  ledger.py             the agent's own books: state, the compute bill, the journal
  config.py             one TOML file, validated once
config.example.toml     a worked example, commented
Dockerfile              the image this repository publishes
```

Built on [`merkl-sdk`](https://github.com/ramcav/merkl-sdk) — the policy
engine, the signer client, the receipt builder and the XRPL settlement adapter
all come from there. This repository is the one thing on top: a loop, a
market reader, and a model.

## The idea

Give an agent real money and real freedom, and put a rule it cannot argue with
between it and the ledger.

The policy lives in a co-signer the agent does not control. The agent is told
which policy *version* its intents must name, and **nothing about what that
policy says** — no cap, no window, no threshold, no destination list. It finds
out what it may do the way anyone finds out a rule: it proposes something, it
is refused in words, and the refusal is handed back to it on the next cycle.
Every one of those refusals is a receipt too, so an outsider can check that
the agent was bounded rather than take somebody's word for it.

That is why `config.py` has no section for the rules and never will. An agent
that could read its own limits would be an agent whose story about staying
inside them is worth nothing.

## What it runs on: five files

Everything this agent needs to exist is a directory of five files — the
"agent bundle" `merkl treasury init` writes for you, in the `merkl-sdk`
repository:

```
merkl-agent/
  trader.toml           the config, filled in from config.example.toml
  agent-ed25519.pem     0600 — this agent's request key
  wallet.json           0600 — this agent's wallet, and no other
  relay-token.txt       0600 — the signer bearer, this agent's own
  notary-api-key.txt    0600 — the org API key minted at enrolment
```

Every path inside `trader.toml` is relative, so the folder can be moved,
bind-mounted or downloaded from the dashboard and still describe itself. The
treasury's own seed is never in it — the master key is disabled, but a seed
is a seed, and this bundle is not where it lives.

## What you need before you start

1. **A treasury.** `merkl treasury init --xrpl-mainnet --trust RLUSD.<issuer>`
   (in `merkl-sdk`) writes the multisigned account, the wallet file and the
   bundle above. Fund it with the hundred dollars you are prepared to lose.
2. **A policy, signed by you, running in a co-signer.** It must grant the
   agent `may_swap`, allow both assets, and allow the operator address you
   want the compute bill paid to. Everything else — the cap, the window, the
   human threshold — is yours to choose and the agent's to discover.
3. **A notary key.** `MERKL_API_KEY` (or the bundle's `notary-api-key.txt`)
   for api.merkl.ai, so the receipts are witnessed somewhere other than your
   laptop.
4. **A model key.** `ANTHROPIC_API_KEY` by default, or `OPENAI_API_KEY` if
   `[model].provider = "openai"` — either way, whichever `[model].api_key_env`
   names in your config.

What `merkl treasury init` cannot fill in is `treasury.policy_version` — that
is decided when you publish the policy — so it says
`"<set this after you publish>"` and the signer refuses every intent until you
replace it.

## Run it

With Docker — the published image, one line:

```bash
docker run --rm \
  -v "$PWD/merkl-agent:/agent:ro" -v merkl-trader:/var/lib/merkl-trader \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  ghcr.io/ramcav/merkl-trader:0.1.0
```

Set `OPENAI_API_KEY` instead when `trader.toml`'s `[model].provider =
"openai"` — both SDKs are in the image either way; only the one the config
names is called.

`./merkl-agent` is the bundle above, mounted read-only. Its journal and its
state go to the named volume instead — `$MERKL_TRADER_HOME`
(`merkl_trader/config.py`) is set to `/var/lib/merkl-trader` in the image,
because `trader.toml` itself still names `~/.merkl/trader` and a read-only
`/agent` cannot be edited to say otherwise.

From a checkout, without Docker:

```bash
python -m merkl_trader --config trader.toml            # the loop
python -m merkl_trader --config trader.toml --once     # one cycle, then exit
python -m merkl_trader --config trader.toml --dry-run  # decide and journal, propose nothing
```

Either way it is a plain process. Put it under whatever already restarts
things — systemd, a supervisor, `restart: unless-stopped` — and do not give
it a scheduler of its own.

## Running it on the box

The server that runs `merkl-api` also runs this, under a compose profile a
plain `docker compose up -d` never starts:

```bash
docker compose -f docker-compose.prod.yml --profile trader up -d trader
```

That service, its volumes and its model key — `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY`, whichever the bundled `trader.toml` names — are defined in
`merkl-api`'s `docker-compose.prod.yml` and `.env.example` — see that
repository's README for the three lines to start it and how to read its
journal from the host.

## What one cycle does

1. **Finish what was in flight.** If the process died between proposing
   something and writing it down, the receipt id and nonce are on disk. The
   agent looks the receipt up and journals what happened. It never re-sends.
   A trading agent that retries on restart is a trading agent that
   double-trades on a bad afternoon.
2. **Resolve an escalation**, if a proposal is sitting with a human. Nothing
   new is decided while somebody is still thinking about the last thing — an
   agent that carried on regardless would be an agent whose escalations mean
   nothing.
3. **Read the market**: the book both ways off the ledger over JSON-RPC, the
   treasury's balances, and one external reference price with its source
   named. The reference is allowed to be down; the cycle still runs.
4. **Pay the compute bill**, if it is due. That is the cycle's one action.
5. **Otherwise ask the model**, and act on at most one thing it says.
6. **Journal, and sleep.**

The cap of one action per cycle is not a rule the model is asked to respect.
The tool loop in `decide.py` stops the instant a proposal is made, so a model
that called `propose_swap` three times would still get exactly one proposal
made.

## What the model sees

Four tools and nothing else:

| tool | what it does |
|---|---|
| `get_market()` | the book, the balances, the reference price |
| `read_receipts(limit)` | its own recent proposals, decisions and refusals |
| `propose_swap(sell_asset, sell_max_amount, buy_asset, buy_amount, reasoning)` | ends the cycle |
| `propose_payment(destination, amount, asset, reasoning)` | ends the cycle |

The system prompt is the `SYSTEM` constant at the top of `decide.py`. It
states the job — stay in business, pay the bill, grow the treasury — says
that doing nothing is a real action, says that the policy exists and cannot
be read, and says that every proposal becomes a public receipt. It states no
number from the policy, and a test asserts that it never will.

Amounts are decimal strings everywhere, from the JSON-RPC parse to the
intent. `0.1 + 0.2` is a bug in a payments system, and no float is ever
built.

## The compute bill

The agent counts its own tokens from what the API reported, prices them at
the rate in `[model]`, and once a week proposes to pay the operator for them
out of the treasury, converted at the price it can see. If that payment is
refused, or the treasury cannot cover it, the agent writes **out of
business** to the journal and exits non-zero.

This is the honest half of the story. The agent is not free, it knows what it
costs, and it stops rather than quietly running on somebody else's money.
Runway — days of compute the treasury can still pay for at the current burn —
is on the first line of every journal entry.

## The journal

`journal.md` gets two lines a cycle:

```
2026-09-10T00:08:45Z — 97.99991 XRP + 200 TST — runway unknown
Bought 100 TST for at most 2 XRP. Settled. receipt b97b9f53858e3d6b6050c242cc1fd86b
```

`journal.jsonl` gets the same facts as data. Neither is evidence and neither
pretends to be — the receipt is the evidence, in `receipts/` and at the
notary. The journal is the part a human reads over coffee.

## Replacing the decision function

`decide.decide()` takes a `ModelPort` — one method, `create(**request)` — and
returns a `Decision`: a swap, a payment, or a considered nothing, plus the
reasoning that becomes the receipt's leaf 6 and the token counts that become
the bill. Write a moving average, a human at a prompt, a different provider;
as long as it returns a `Decision`, the rest of the program does not change
and does not care.

## Tests

```bash
pytest -q
```

Everything below the model is real there: a real signer with a real
encrypted keystore and real sealed window state, real receipts on disk,
against `merkl-sdk`'s in-memory rail. Two things are stubbed — the model (a
scripted list of replies) and the XRPL node (`httpx.MockTransport`, so
`market.py`'s own JSON-RPC parsing is exercised rather than skipped).

```bash
ruff check merkl_trader tests
ruff format --check merkl_trader tests
```
