#!/usr/bin/env bash
# Drive entrypoint.sh until just before exec btqd.
set -u
HOST="${1:-btq-node-0}"
NODES="${2:-3}"
NETWORK="${3:-regtest}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENTRY="$ROOT/docker/node/entrypoint.sh"
if [ ! -f "$ENTRY" ]; then
  echo "missing entrypoint" >&2
  exit 1
fi

# Rewrite exec to echo ARGS instead of launching btqd.
tmp="$(mktemp)"
sed 's/^exec /echo EXEC /' "$ENTRY" > "$tmp"
chmod +x "$tmp"

export HOSTNAME="$HOST"
export BTQ_NODES="$NODES"
export BTQ_NETWORK="$NETWORK"
export BTQ_RPC_USER=btq
export BTQ_RPC_PASSWORD=btq
export BTQ_HEADLESS=btq-headless
export BTQ_NAMESPACE=btqnet
export BTQ_DNS_SEED=btq-headless
export BTQ_PRUNE="${BTQ_PRUNE:-0}"
export BTQ_MAX_CONNECTIONS="${BTQ_MAX_CONNECTIONS:-32}"
export BTQ_EXTRA_ARGS="${BTQ_EXTRA_ARGS:-}"

# host / ip binaries used by the script — stub them.
export PATH="$(dirname "$0")/stubs:$PATH"
mkdir -p "$(dirname "$0")/stubs"
printf '#!/bin/sh\nexit 0\n' > "$(dirname "$0")/stubs/host"
printf '#!/bin/sh\necho 127.0.0.1\n' > "$(dirname "$0")/stubs/hostname"
chmod +x "$(dirname "$0")/stubs/host" "$(dirname "$0")/stubs/hostname"

timeout 1.5 bash "$tmp" || code=$?
rm -f "$tmp"
exit "${code:-0}"
