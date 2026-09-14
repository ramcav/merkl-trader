# The reference trading agent as a container: python:3.12-slim, this package
# and its dependencies (merkl-sdk[xrpl], anthropic), nothing else.
#
#   docker run --rm \
#     -v "$PWD/merkl-agent:/agent:ro" -v merkl-trader:/var/lib/merkl-trader \
#     -e ANTHROPIC_API_KEY=sk-ant-... \
#     ghcr.io/ramcav/merkl-trader:0.1.0
#
# /agent is read-only: the trader reads its five files there (trader.toml,
# agent-ed25519.pem, wallet.json, relay-token.txt, notary-api-key.txt — the
# bundle `merkl treasury init` writes, in the merkl-sdk repository) and writes
# nothing back. Its journal and its state go to /var/lib/merkl-trader instead;
# $MERKL_TRADER_HOME (merkl_trader/config.py) is set to that volume here,
# because trader.toml itself still names ~/.merkl/trader and a read-only
# /agent cannot be edited to say otherwise.
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/ramcav/merkl-trader" \
      org.opencontainers.image.description="Merkl reference trading agent"

COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src

# Unprivileged from the start: unlike the signer image, there is no
# host-owned bind mount to chown before this starts — /agent arrives
# read-only.
RUN useradd --system --uid 10002 --user-group --no-create-home merkl-trader \
    && mkdir -p /var/lib/merkl-trader /agent \
    && chown merkl-trader:merkl-trader /var/lib/merkl-trader
USER merkl-trader

# PYTHONUNBUFFERED so the two journal lines each cycle reach `docker logs`
# when they are printed rather than when the process ends.
ENV MERKL_TRADER_HOME=/var/lib/merkl-trader \
    PYTHONUNBUFFERED=1

VOLUME ["/var/lib/merkl-trader"]

ENTRYPOINT ["python", "-m", "merkl_trader"]
CMD ["--config", "/agent/trader.toml"]
