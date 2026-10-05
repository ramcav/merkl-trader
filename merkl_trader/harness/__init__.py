"""The harness agent: the same mandate, journal and interval as ``merkl_trader``,
decided by one run of an OpenAI Agents SDK agent with real tools instead of the
four hand-rolled ones in ``decide.py``.

    python -m merkl_trader.harness --config trader.toml

See ``loop.py`` for the whole story. ``python -m merkl_trader`` (``trader.py``)
is still here too — the "no framework" reference this harness is measured
against, not something it replaces.
"""
