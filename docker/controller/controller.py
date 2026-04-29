"""BTQnet controller: metrics exporter + traffic generator.

Runs three concurrent loops in a single process:

1. Scrape loop  — every SCRAPE_INTERVAL seconds, queries every node's RPC
                  for block height, peer info, mempool, net totals, chaintips,
                  and re-publishes the result as Prometheus metrics on
                  http://0.0.0.0:9100/metrics.

2. Mining loop  — every BLOCK_INTERVAL seconds, generates 1 block on a
                  rotating node, mining to a stable regtest address derived
                  from node-0's wallet. Bootstraps with 101 blocks on the
                  first iteration so the coinbase matures.

3. TX loop      — every TX_INTERVAL seconds, sends a tiny random amount from
                  the funded wallet on node-0 to a random address on a random
                  other node. Provides a constant flow of traffic for mempool
                  propagation analysis.

All three loops are best-effort: any RPC error is logged and re-tried on the
next tick. The exporter never crashes the process for transient failures.
"""

from __future__ import annotations

import json
import logging
import os
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests
from prometheus_client import (
    CollectorRegistry,
    Gauge,
    Counter,
    start_http_server,
)

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
NODES = int(os.environ.get("BTQ_NODES", "5"))
HEADLESS = os.environ.get("BTQ_HEADLESS", "btq-headless")
NAMESPACE = os.environ.get("BTQ_NAMESPACE", "btqnet")
NETWORK = os.environ.get("BTQ_NETWORK", "regtest")
RPC_USER = os.environ.get("BTQ_RPC_USER", "btq")
RPC_PASS = os.environ.get("BTQ_RPC_PASSWORD", "btq")
RPC_PORT = int(os.environ.get("BTQ_RPC_PORT", "18443"))

SCRAPE_INTERVAL = float(os.environ.get("SCRAPE_INTERVAL", "3"))
BLOCK_INTERVAL = float(os.environ.get("BLOCK_INTERVAL", "10"))
TX_INTERVAL = float(os.environ.get("TX_INTERVAL", "4"))
BOOTSTRAP_BLOCKS = int(os.environ.get("BOOTSTRAP_BLOCKS", "101"))
ENABLE_MINING = os.environ.get("ENABLE_MINING", "1") == "1"
ENABLE_TX = os.environ.get("ENABLE_TX", "1") == "1"
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))

# Controller domain — used to make every node's "node" label stable.
NODE_PREFIX = os.environ.get("BTQ_NODE_PREFIX", "btq-node")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("btqnet-controller")


def node_dns(i: int) -> str:
    """Return the in-cluster DNS for node `i`."""
    return f"{NODE_PREFIX}-{i}.{HEADLESS}"


# --------------------------------------------------------------------------- #
# Tiny BTQ JSON-RPC client                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class RPCError(Exception):
    """Raised when btqd returns an RPC error or HTTP request fails."""

    code: int
    message: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"RPCError({self.code}): {self.message}"


@dataclass
class RPC:
    host: str
    port: int = RPC_PORT
    user: str = RPC_USER
    password: str = RPC_PASS
    timeout: float = 5.0
    session: requests.Session = field(default_factory=requests.Session)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def call(
        self,
        method: str,
        *params: Any,
        wallet: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = self.url
        if wallet:
            url = f"{url}wallet/{wallet}"
        payload = {
            "jsonrpc": "1.0",
            "id": "btqnet",
            "method": method,
            "params": list(params),
        }
        try:
            r = self.session.post(
                url,
                data=json.dumps(payload),
                auth=(self.user, self.password),
                timeout=timeout if timeout is not None else self.timeout,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as e:
            raise RPCError(code=-1, message=f"transport: {e}") from e
        # btqd returns 500 with JSON body on RPC errors — we still parse it
        try:
            data = r.json()
        except ValueError:
            raise RPCError(code=r.status_code, message=r.text[:200])
        if data.get("error"):
            err = data["error"]
            raise RPCError(code=err.get("code", -1), message=err.get("message", ""))
        return data.get("result")


# --------------------------------------------------------------------------- #
# Prometheus metrics                                                          #
# --------------------------------------------------------------------------- #

REG = CollectorRegistry()

M_UP = Gauge(
    "btq_node_up", "1 if the node's RPC is reachable", ["node"], registry=REG
)
M_VERSION = Gauge(
    "btq_node_version_info",
    "Static gauge with version label set, value=1",
    ["node", "version", "subversion"],
    registry=REG,
)
M_BLOCKS = Gauge(
    "btq_node_blocks", "Block count", ["node"], registry=REG
)
M_HEADERS = Gauge(
    "btq_node_headers", "Header count", ["node"], registry=REG
)
M_DIFFICULTY = Gauge(
    "btq_node_difficulty", "Current difficulty", ["node"], registry=REG
)
M_VERIFICATION = Gauge(
    "btq_node_verification_progress",
    "Initial-block-download verification progress 0..1",
    ["node"], registry=REG,
)
M_PEERS = Gauge(
    "btq_node_peer_count",
    "Connected peers, split by direction",
    ["node", "direction"],
    registry=REG,
)
M_PING = Gauge(
    "btq_node_peer_ping_seconds",
    "Last ping RTT to peer, in seconds",
    ["node", "peer"],
    registry=REG,
)
M_PEER_HEIGHT = Gauge(
    "btq_node_peer_starting_height",
    "Reported starting height for each peer",
    ["node", "peer"],
    registry=REG,
)
M_MEM_TX = Gauge(
    "btq_node_mempool_tx",
    "Transactions in mempool",
    ["node"], registry=REG,
)
M_MEM_BYTES = Gauge(
    "btq_node_mempool_bytes",
    "Bytes in mempool",
    ["node"], registry=REG,
)
M_MEM_MIN_FEE = Gauge(
    "btq_node_mempool_min_fee",
    "Mempool minimum fee (BTQ/kvB)",
    ["node"], registry=REG,
)
M_NET_RECV = Gauge(
    "btq_node_bytes_recv_total",
    "Total bytes received over P2P",
    ["node"], registry=REG,
)
M_NET_SENT = Gauge(
    "btq_node_bytes_sent_total",
    "Total bytes sent over P2P",
    ["node"], registry=REG,
)
M_TIPS = Gauge(
    "btq_node_chain_tips",
    "Number of chain tips by status",
    ["node", "status"],
    registry=REG,
)
M_BEST_BLOCK_HEIGHT = Gauge(
    "btq_node_best_block_height",
    "Height of node's best block",
    ["node"],
    registry=REG,
)
M_UPTIME = Gauge(
    "btq_node_uptime_seconds", "Node uptime in seconds",
    ["node"], registry=REG,
)

# Cluster-wide rollups
M_CLUSTER_HEIGHT_MAX = Gauge(
    "btq_cluster_height_max",
    "Maximum block height across all reachable nodes",
    registry=REG,
)
M_CLUSTER_HEIGHT_MIN = Gauge(
    "btq_cluster_height_min",
    "Minimum block height across all reachable nodes",
    registry=REG,
)
M_CLUSTER_HEIGHT_SPREAD = Gauge(
    "btq_cluster_height_spread",
    "max - min height across reachable nodes",
    registry=REG,
)
M_CLUSTER_DISTINCT_TIPS = Gauge(
    "btq_cluster_distinct_best_blocks",
    "Number of distinct best-block hashes across the cluster",
    registry=REG,
)
M_CLUSTER_NODES_REACHABLE = Gauge(
    "btq_cluster_nodes_reachable",
    "How many nodes are RPC-reachable",
    registry=REG,
)
M_CLUSTER_NODES_TOTAL = Gauge(
    "btq_cluster_nodes_total",
    "Total nodes the controller is configured for",
    registry=REG,
)

# Block-propagation timing: when did each node first see each block?
M_PROPAGATION = Gauge(
    "btq_block_propagation_seconds",
    "Time from a block first being seen anywhere to being seen on this node",
    ["node"],
    registry=REG,
)
M_PROPAGATION_LAST = Gauge(
    "btq_block_propagation_last_seconds",
    "Most recent block propagation time observed for this node",
    ["node"],
    registry=REG,
)

C_BLOCKS_MINED = Counter(
    "btq_controller_blocks_mined_total",
    "Blocks generated by the controller",
    ["node"],
    registry=REG,
)
C_TX_SENT = Counter(
    "btq_controller_tx_sent_total",
    "Transactions sent by the controller",
    ["src_node", "dst_node"],
    registry=REG,
)
C_TX_FAILED = Counter(
    "btq_controller_tx_failed_total",
    "Transactions the controller failed to send",
    ["src_node", "reason"],
    registry=REG,
)


# --------------------------------------------------------------------------- #
# Shared state                                                                #
# --------------------------------------------------------------------------- #


class State:
    """Tiny shared state container, locked for cross-thread access."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # block_hash -> {first_seen_ts, seen_on: {node_idx: ts}}
        self.blocks: dict[str, dict[str, Any]] = {}
        # cached node addresses (per node, freshly-derived once)
        self.addresses: dict[int, str] = {}
        # whether bootstrap has completed
        self.bootstrapped = False


STATE = State()


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def rpc_for(idx: int) -> RPC:
    return RPC(host=node_dns(idx))


def ensure_wallet(idx: int) -> bool:
    """Make sure node `idx` has a "default" wallet loaded. Idempotent."""
    rpc = rpc_for(idx)
    try:
        wallets = rpc.call("listwallets")
        if "default" in wallets:
            return True
    except RPCError as e:
        log.debug("listwallets on node-%d failed: %s", idx, e)
        return False
    # try loadwallet first (re-attaches an existing wallet directory)
    try:
        rpc.call("loadwallet", "default")
        return True
    except RPCError as e:
        if e.code != -18:  # -18 = wallet not found
            log.debug("loadwallet default on node-%d: %s", idx, e)
    try:
        rpc.call("createwallet", "default", False, False, "", False, True, True)
        return True
    except RPCError as e:
        if e.code == -4 and "already exists" in e.message.lower():
            return True
        log.warning("createwallet on node-%d failed: %s", idx, e)
        return False


def address_for(idx: int) -> str | None:
    with STATE.lock:
        cached = STATE.addresses.get(idx)
    if cached:
        return cached
    if not ensure_wallet(idx):
        return None
    try:
        addr = rpc_for(idx).call("getnewaddress", "btqnet", "bech32", wallet="default")
    except RPCError as e:
        log.warning("getnewaddress on node-%d: %s", idx, e)
        return None
    with STATE.lock:
        STATE.addresses[idx] = addr
    log.info("cached address for node-%d: %s", idx, addr)
    return addr


# --------------------------------------------------------------------------- #
# Scrape loop                                                                 #
# --------------------------------------------------------------------------- #


def _record_block_seen(idx: int, block_hash: str, height: int, now: float) -> None:
    """Track first-seen times so we can compute propagation latency."""
    with STATE.lock:
        rec = STATE.blocks.get(block_hash)
        if rec is None:
            rec = {"first_seen_ts": now, "height": height, "seen_on": {}}
            STATE.blocks[block_hash] = rec
            # Trim history so the dict doesn't grow unbounded
            if len(STATE.blocks) > 2048:
                # drop oldest by first_seen_ts
                oldest = sorted(STATE.blocks.items(),
                                key=lambda kv: kv[1]["first_seen_ts"])[:512]
                for h, _ in oldest:
                    STATE.blocks.pop(h, None)
        if idx not in rec["seen_on"]:
            rec["seen_on"][idx] = now
            delta = now - rec["first_seen_ts"]
            label = f"{NODE_PREFIX}-{idx}"
            M_PROPAGATION.labels(node=label).set(delta)
            M_PROPAGATION_LAST.labels(node=label).set(delta)


def scrape_node(idx: int) -> dict[str, Any] | None:
    """Pull all the metric inputs from one node and update Prometheus metrics."""
    rpc = rpc_for(idx)
    label = f"{NODE_PREFIX}-{idx}"
    out: dict[str, Any] = {}
    try:
        net = rpc.call("getnetworkinfo")
        chain = rpc.call("getblockchaininfo")
        peers = rpc.call("getpeerinfo")
        mempool = rpc.call("getmempoolinfo")
        nettotals = rpc.call("getnettotals")
        chaintips = rpc.call("getchaintips")
        uptime = rpc.call("uptime")
    except RPCError as e:
        log.debug("scrape node-%d: %s", idx, e)
        M_UP.labels(node=label).set(0)
        return None

    M_UP.labels(node=label).set(1)
    M_VERSION.labels(
        node=label,
        version=str(net.get("version", "")),
        subversion=net.get("subversion", ""),
    ).set(1)
    M_BLOCKS.labels(node=label).set(chain.get("blocks", 0))
    M_HEADERS.labels(node=label).set(chain.get("headers", 0))
    M_DIFFICULTY.labels(node=label).set(chain.get("difficulty", 0))
    M_VERIFICATION.labels(node=label).set(chain.get("verificationprogress", 0))
    M_BEST_BLOCK_HEIGHT.labels(node=label).set(chain.get("blocks", 0))
    M_UPTIME.labels(node=label).set(uptime or 0)

    inbound = sum(1 for p in peers if p.get("inbound"))
    outbound = sum(1 for p in peers if not p.get("inbound"))
    M_PEERS.labels(node=label, direction="inbound").set(inbound)
    M_PEERS.labels(node=label, direction="outbound").set(outbound)
    M_PEERS.labels(node=label, direction="total").set(len(peers))

    for p in peers:
        peer_id = p.get("addr", "?")
        if "pingtime" in p:
            M_PING.labels(node=label, peer=peer_id).set(p["pingtime"])
        if "startingheight" in p:
            M_PEER_HEIGHT.labels(node=label, peer=peer_id).set(p["startingheight"])

    M_MEM_TX.labels(node=label).set(mempool.get("size", 0))
    M_MEM_BYTES.labels(node=label).set(mempool.get("bytes", 0))
    M_MEM_MIN_FEE.labels(node=label).set(mempool.get("mempoolminfee", 0))

    M_NET_RECV.labels(node=label).set(nettotals.get("totalbytesrecv", 0))
    M_NET_SENT.labels(node=label).set(nettotals.get("totalbytessent", 0))

    # chaintips: clear previous statuses for this node first by relabel-set
    tip_buckets: dict[str, int] = {}
    for tip in chaintips:
        tip_buckets[tip["status"]] = tip_buckets.get(tip["status"], 0) + 1
    for status in ("active", "valid-fork", "valid-headers",
                   "headers-only", "invalid", "unknown"):
        M_TIPS.labels(node=label, status=status).set(tip_buckets.get(status, 0))

    # propagation tracking — find the active tip
    active_tip_hash = None
    active_tip_height = 0
    for tip in chaintips:
        if tip["status"] == "active":
            active_tip_hash = tip["hash"]
            active_tip_height = tip["height"]
            break
    if active_tip_hash:
        _record_block_seen(idx, active_tip_hash, active_tip_height, time.time())
        out["best_hash"] = active_tip_hash
        out["best_height"] = active_tip_height

    out["blocks"] = chain.get("blocks", 0)
    out["peers"] = len(peers)
    return out


def cluster_rollup(snapshots: list[dict[str, Any] | None]) -> None:
    reachable = [s for s in snapshots if s is not None]
    M_CLUSTER_NODES_REACHABLE.set(len(reachable))
    M_CLUSTER_NODES_TOTAL.set(NODES)
    if not reachable:
        return
    heights = [s["blocks"] for s in reachable]
    M_CLUSTER_HEIGHT_MAX.set(max(heights))
    M_CLUSTER_HEIGHT_MIN.set(min(heights))
    M_CLUSTER_HEIGHT_SPREAD.set(max(heights) - min(heights))
    distinct = {s.get("best_hash") for s in reachable if s.get("best_hash")}
    M_CLUSTER_DISTINCT_TIPS.set(len(distinct))


def scrape_loop() -> None:
    log.info("scrape loop starting (interval=%.1fs, nodes=%d)",
             SCRAPE_INTERVAL, NODES)
    while True:
        t0 = time.time()
        snaps: list[dict[str, Any] | None] = []
        for idx in range(NODES):
            snaps.append(scrape_node(idx))
        cluster_rollup(snaps)
        elapsed = time.time() - t0
        sleep = max(0.1, SCRAPE_INTERVAL - elapsed)
        time.sleep(sleep)


# --------------------------------------------------------------------------- #
# Mining loop                                                                 #
# --------------------------------------------------------------------------- #


def wait_for_any_node() -> int | None:
    """Block until at least one node responds. Returns its idx."""
    log.info("waiting for first reachable node ...")
    deadline = time.time() + 600
    while time.time() < deadline:
        for idx in range(NODES):
            try:
                rpc_for(idx).call("getblockchaininfo")
                log.info("node-%d is reachable", idx)
                return idx
            except RPCError:
                pass
        time.sleep(2)
    return None


def bootstrap() -> None:
    """Mine BOOTSTRAP_BLOCKS blocks to a node-0 address so coinbases mature.

    Mines in chunks so a single RPC doesn't time out, and idempotently
    skips any chunks already mined (e.g. if the controller restarts).
    """
    if not ENABLE_MINING:
        return
    first = wait_for_any_node()
    if first is None:
        log.error("no nodes reachable; bootstrap aborted")
        return
    addr = address_for(0) or address_for(first)
    if not addr:
        log.error("could not derive bootstrap address; mining disabled")
        return

    # If node-0 already has spendable funds, skip; otherwise mine
    # BOOTSTRAP_BLOCKS fresh blocks to it.  This handles the case where
    # the chain is already advanced (e.g. controller restart) but
    # node-0's wallet was reset (StatefulSet roll on emptyDir).
    try:
        bal = rpc_for(0).call("getbalance", wallet="default", timeout=10.0)
    except RPCError:
        bal = 0.0
    if bal and bal > 1.0:
        log.info("bootstrap: node-0 already has spendable balance %.4f, skipping", bal)
        with STATE.lock:
            STATE.bootstrapped = True
        return

    try:
        start_height = rpc_for(0).call("getblockcount", timeout=10.0)
    except RPCError:
        start_height = 0
    target = start_height + BOOTSTRAP_BLOCKS
    log.info("bootstrap: mining %d blocks on node-0 to %s (start_height=%d -> target=%d)",
             BOOTSTRAP_BLOCKS, addr, start_height, target)

    chunk = 25
    height = start_height
    while height < target:
        n = min(chunk, target - height)
        try:
            rpc_for(0).call("generatetoaddress", n, addr, timeout=120.0)
            C_BLOCKS_MINED.labels(node=f"{NODE_PREFIX}-0").inc(n)
        except RPCError as e:
            log.warning("bootstrap chunk failed (will retry): %s", e)
            time.sleep(2)
        try:
            height = rpc_for(0).call("getblockcount", timeout=10.0)
        except RPCError:
            time.sleep(2)
            continue
        log.info("bootstrap progress: height=%d/%d", height, target)
    with STATE.lock:
        STATE.bootstrapped = True
    log.info("bootstrap complete (height=%d)", height)


def mining_loop() -> None:
    if not ENABLE_MINING:
        log.info("mining disabled")
        return
    bootstrap()
    miner_idx = 0
    while True:
        time.sleep(BLOCK_INTERVAL)
        miner_idx = (miner_idx + 1) % NODES
        addr = address_for(miner_idx) or address_for(0)
        if not addr:
            continue
        try:
            rpc_for(miner_idx).call("generatetoaddress", 1, addr, timeout=30.0)
            C_BLOCKS_MINED.labels(node=f"{NODE_PREFIX}-{miner_idx}").inc()
            log.info("mined block on node-%d", miner_idx)
        except RPCError as e:
            log.debug("generatetoaddress on node-%d: %s", miner_idx, e)


# --------------------------------------------------------------------------- #
# Tx loop                                                                     #
# --------------------------------------------------------------------------- #


def tx_loop() -> None:
    if not ENABLE_TX:
        log.info("tx generation disabled")
        return
    # wait until bootstrap finished so the source wallet has spendable coins
    while True:
        with STATE.lock:
            if STATE.bootstrapped:
                break
        time.sleep(2)
    log.info("tx loop starting (interval=%.1fs)", TX_INTERVAL)

    src_idx = 0  # node-0 has the bootstrap funds
    while True:
        time.sleep(TX_INTERVAL)
        dst_idx = random.randrange(NODES)
        if dst_idx == src_idx and NODES > 1:
            dst_idx = (src_idx + 1) % NODES
        dst_addr = address_for(dst_idx)
        if not dst_addr:
            C_TX_FAILED.labels(
                src_node=f"{NODE_PREFIX}-{src_idx}", reason="no_dst_addr",
            ).inc()
            continue
        amount = round(random.uniform(0.0001, 0.01), 8)
        try:
            rpc_for(src_idx).call(
                "sendtoaddress", dst_addr, amount,
                wallet="default", timeout=15.0,
            )
            C_TX_SENT.labels(
                src_node=f"{NODE_PREFIX}-{src_idx}",
                dst_node=f"{NODE_PREFIX}-{dst_idx}",
            ).inc()
        except RPCError as e:
            log.debug("sendtoaddress %d->%d: %s", src_idx, dst_idx, e)
            C_TX_FAILED.labels(
                src_node=f"{NODE_PREFIX}-{src_idx}",
                reason=f"rpc_{e.code}",
            ).inc()


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #


def main() -> None:
    log.info(
        "btqnet controller starting :: nodes=%d headless=%s rpc_port=%d "
        "scrape=%.1fs block=%.1fs tx=%.1fs",
        NODES, HEADLESS, RPC_PORT, SCRAPE_INTERVAL, BLOCK_INTERVAL, TX_INTERVAL,
    )
    M_CLUSTER_NODES_TOTAL.set(NODES)
    start_http_server(METRICS_PORT, registry=REG)
    log.info("prometheus exporter listening on :%d/metrics", METRICS_PORT)
    threading.Thread(target=scrape_loop, daemon=True, name="scrape").start()
    threading.Thread(target=mining_loop, daemon=True, name="mining").start()
    threading.Thread(target=tx_loop, daemon=True, name="tx").start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
