#!/usr/bin/env bash
# Entrypoint for a BTQ node pod inside the btqnet StatefulSet.
#
# Behaviour
#   - Reads the StatefulSet ordinal from $HOSTNAME (e.g. "btq-node-3")
#   - Builds an -addnode list pointing at every other node via the
#     headless service ($BTQ_HEADLESS, default "btq-headless")
#   - Waits for at least one peer's DNS record to resolve before starting
#   - Starts btqd in regtest with the analytics knobs turned all the way up
#
# Required env (StatefulSet provides all of these via env / fieldRef):
#   BTQ_NODES         total number of nodes in the StatefulSet (e.g. 5)
#   BTQ_NETWORK       chain to use: regtest|signet|test|main (default regtest)
#   BTQ_RPC_USER      RPC username  (default: btq)
#   BTQ_RPC_PASSWORD  RPC password  (default: btq)
#   BTQ_HEADLESS      headless service name (default: btq-headless)
#   BTQ_NAMESPACE     k8s namespace (default: btqnet)
#   BTQ_EXTRA_ARGS    extra raw args appended to the btqd invocation
set -euo pipefail

NODES="${BTQ_NODES:-5}"
NETWORK="${BTQ_NETWORK:-regtest}"
RPC_USER="${BTQ_RPC_USER:-btq}"
RPC_PASS="${BTQ_RPC_PASSWORD:-btq}"
HEADLESS="${BTQ_HEADLESS:-btq-headless}"
NAMESPACE="${BTQ_NAMESPACE:-btqnet}"
EXTRA="${BTQ_EXTRA_ARGS:-}"

# Resolve our ordinal from the pod hostname (e.g. btq-node-7 -> 7).
HOSTNAME_SHORT="${HOSTNAME:-$(hostname)}"
ORDINAL="${HOSTNAME_SHORT##*-}"
if ! [[ "$ORDINAL" =~ ^[0-9]+$ ]]; then
    echo "[entrypoint] FATAL: cannot parse ordinal from hostname '$HOSTNAME_SHORT'" >&2
    exit 1
fi

DATADIR="/var/lib/btq/data"
mkdir -p "$DATADIR"

# Per-network ports
case "$NETWORK" in
    regtest) P2P_PORT=18444; RPC_PORT=18443 ;;
    test)    P2P_PORT=18333; RPC_PORT=18332 ;;
    signet)  P2P_PORT=38333; RPC_PORT=38332 ;;
    main)    P2P_PORT=8333;  RPC_PORT=8332  ;;
    *) echo "[entrypoint] unknown BTQ_NETWORK=$NETWORK" >&2; exit 1 ;;
esac

# Build the -addnode list (every node that isn't us).
ADDNODES=()
for i in $(seq 0 $((NODES-1))); do
    if [ "$i" -ne "$ORDINAL" ]; then
        ADDNODES+=( "-addnode=btq-node-${i}.${HEADLESS}:${P2P_PORT}" )
    fi
done

# Wait until at least one peer pod's DNS record is resolvable so the
# initial outbound connection attempts actually succeed.  StatefulSet
# pods come up in ordinal order, so node 0 won't have peers yet — that
# is fine, peers will dial in once they're up.
if [ "$NODES" -gt 1 ] && [ "$ORDINAL" -gt 0 ]; then
    target="btq-node-0.${HEADLESS}"
    echo "[entrypoint] node ${ORDINAL}/${NODES}: waiting for ${target} ..."
    for _ in $(seq 1 60); do
        if host "$target" >/dev/null 2>&1; then
            echo "[entrypoint] ${target} resolvable"
            break
        fi
        sleep 2
    done
fi

# btqd flags
#   - regtest with "-fallbackfee" so wallet-driven tx generation works
#   - listen on all ifaces, bind RPC to 0.0.0.0 with cluster-wide allow-list
#   - all five ZMQ topics published on :28332/28333 for downstream consumers
#   - verbose debug logging that hits the categories we actually care about
ARGS=(
    "-chain=${NETWORK}"
    "-datadir=${DATADIR}"
    "-server=1"
    "-listen=1"
    "-listenonion=0"
    "-discover=0"
    "-dnsseed=0"
    "-upnp=0"
    "-natpmp=0"
    "-bind=0.0.0.0:${P2P_PORT}"
    "-rpcbind=0.0.0.0:${RPC_PORT}"
    "-rpcallowip=0.0.0.0/0"
    "-rpcuser=${RPC_USER}"
    "-rpcpassword=${RPC_PASS}"
    "-rpcworkqueue=64"
    "-rpcthreads=8"
    "-fallbackfee=0.00001"
    "-txindex=1"
    "-blockfilterindex=1"
    "-coinstatsindex=1"
    "-zmqpubrawblock=tcp://0.0.0.0:28332"
    "-zmqpubrawtx=tcp://0.0.0.0:28332"
    "-zmqpubhashblock=tcp://0.0.0.0:28332"
    "-zmqpubhashtx=tcp://0.0.0.0:28332"
    "-zmqpubsequence=tcp://0.0.0.0:28333"
    "-debug=net"
    "-debug=mempool"
    "-debug=mempoolrej"
    "-debug=validation"
    "-debug=blockstorage"
    "-debug=cmpctblock"
    "-printtoconsole=1"
    "-shrinkdebugfile=0"
    "${ADDNODES[@]}"
)

if [ -n "$EXTRA" ]; then
    # shellcheck disable=SC2206
    EXTRA_ARR=( $EXTRA )
    ARGS+=( "${EXTRA_ARR[@]}" )
fi

echo "[entrypoint] node=${HOSTNAME_SHORT} ordinal=${ORDINAL}/${NODES} chain=${NETWORK}"
echo "[entrypoint] launching: btqd ${ARGS[*]}"
exec btqd "${ARGS[@]}"
