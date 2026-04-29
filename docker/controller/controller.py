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

import collections
import json
import logging
import os
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

_DEFAULT_RPC_PORT = {
    "regtest": 18443,
    "test": 18332,
    "testnet": 18332,
    "signet": 38332,
    "main": 8332,
    "mainnet": 8332,
}
RPC_PORT = int(os.environ.get("BTQ_RPC_PORT") or _DEFAULT_RPC_PORT.get(NETWORK, 18443))

SCRAPE_INTERVAL = float(os.environ.get("SCRAPE_INTERVAL", "3"))
BLOCK_INTERVAL = float(os.environ.get("BLOCK_INTERVAL", "10"))
TX_INTERVAL = float(os.environ.get("TX_INTERVAL", "4"))
BOOTSTRAP_BLOCKS = int(os.environ.get("BOOTSTRAP_BLOCKS", "101"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))
UI_PORT = int(os.environ.get("UI_PORT", "9200"))
STATIC_DIR = Path(os.environ.get("STATIC_DIR", "/app/static"))


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.lower() in ("1", "true", "yes", "on")


# Mining and tx-generation only make sense on regtest, where the
# controller can produce blocks instantly via generatetoaddress.  On
# testnet/signet/main, real PoW + real funds are required, so we
# default to "scrape only" but still allow explicit opt-in via env.
_is_regtest = NETWORK == "regtest"
ENABLE_MINING = _bool_env("ENABLE_MINING", _is_regtest)
ENABLE_TX = _bool_env("ENABLE_TX", _is_regtest)
ENABLE_BOOTSTRAP = _bool_env("ENABLE_BOOTSTRAP", _is_regtest)

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
        self.lock = threading.RLock()
        # block_hash -> {first_seen_ts, seen_on: {node_idx: ts}}
        self.blocks: dict[str, dict[str, Any]] = {}
        # cached node addresses (per node, freshly-derived once)
        self.addresses: dict[int, str] = {}
        # whether bootstrap has completed
        self.bootstrapped = False
        # most-recent per-node snapshot from the scrape loop, for the UI
        self.snapshots: dict[int, dict[str, Any]] = {}
        # last cluster rollup
        self.cluster: dict[str, Any] = {}
        # ring buffer of human-readable events (most recent first)
        self.events: collections.deque[dict[str, Any]] = collections.deque(maxlen=200)
        # nodes the user has explicitly isolated via the UI
        self.isolated: set[int] = set()
        # runtime overrides for the auto loops (None = use env default)
        self.mining_enabled: bool | None = None
        self.tx_enabled: bool | None = None


STATE = State()


def is_mining_enabled() -> bool:
    with STATE.lock:
        if STATE.mining_enabled is None:
            return ENABLE_MINING
        return STATE.mining_enabled


def is_tx_enabled() -> bool:
    with STATE.lock:
        if STATE.tx_enabled is None:
            return ENABLE_TX
        return STATE.tx_enabled


def push_event(kind: str, message: str, **extra: Any) -> None:
    evt = {
        "ts": time.time(),
        "kind": kind,
        "msg": message,
    }
    if extra:
        evt.update(extra)
    with STATE.lock:
        STATE.events.appendleft(evt)
    log.info("event: %s :: %s", kind, message)


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
    out: dict[str, Any] = {"idx": idx, "label": label}
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
        with STATE.lock:
            STATE.snapshots[idx] = {"idx": idx, "label": label, "up": False}
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

    out["up"] = True
    out["blocks"] = chain.get("blocks", 0)
    out["headers"] = chain.get("headers", 0)
    out["verification_progress"] = chain.get("verificationprogress", 0)
    out["peers"] = len(peers)
    out["peers_in"] = inbound
    out["peers_out"] = outbound
    out["mempool_tx"] = mempool.get("size", 0)
    out["mempool_bytes"] = mempool.get("bytes", 0)
    out["uptime"] = uptime or 0
    out["network_active"] = net.get("networkactive", True)
    with STATE.lock:
        STATE.snapshots[idx] = out
    return out


def cluster_rollup(snapshots: list[dict[str, Any] | None]) -> None:
    reachable = [s for s in snapshots if s is not None]
    M_CLUSTER_NODES_REACHABLE.set(len(reachable))
    M_CLUSTER_NODES_TOTAL.set(NODES)
    cluster: dict[str, Any] = {
        "reachable": len(reachable),
        "total": NODES,
        "network": NETWORK,
    }
    if reachable:
        heights = [s["blocks"] for s in reachable]
        cluster["height_max"] = max(heights)
        cluster["height_min"] = min(heights)
        cluster["spread"] = max(heights) - min(heights)
        M_CLUSTER_HEIGHT_MAX.set(max(heights))
        M_CLUSTER_HEIGHT_MIN.set(min(heights))
        M_CLUSTER_HEIGHT_SPREAD.set(max(heights) - min(heights))
        distinct = {s.get("best_hash") for s in reachable if s.get("best_hash")}
        cluster["distinct_best_blocks"] = len(distinct)
        M_CLUSTER_DISTINCT_TIPS.set(len(distinct))
    with STATE.lock:
        STATE.cluster = cluster


def scrape_once() -> None:
    """Run a single scrape pass across every node + recompute rollups."""
    snaps: list[dict[str, Any] | None] = []
    for idx in range(NODES):
        snaps.append(scrape_node(idx))
    cluster_rollup(snaps)


def scrape_loop() -> None:
    log.info("scrape loop starting (interval=%.1fs, nodes=%d)",
             SCRAPE_INTERVAL, NODES)
    while True:
        t0 = time.time()
        scrape_once()
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
    Only runs on regtest (or when explicitly forced via ENABLE_BOOTSTRAP).
    """
    if not ENABLE_MINING or not ENABLE_BOOTSTRAP:
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
    """Auto-mining loop. Honours STATE.mining_enabled at every tick so it
    can be paused / resumed at runtime via the UI without restarting."""
    bootstrap()
    miner_idx = 0
    while True:
        time.sleep(BLOCK_INTERVAL)
        if not is_mining_enabled():
            continue
        miner_idx = (miner_idx + 1) % NODES
        # Skip nodes the user has explicitly isolated (their wallets
        # would still mine, but the propagation panel becomes
        # uninterpretable).
        with STATE.lock:
            if miner_idx in STATE.isolated:
                continue
        addr = address_for(miner_idx) or address_for(0)
        if not addr:
            continue
        try:
            rpc_for(miner_idx).call("generatetoaddress", 1, addr, timeout=30.0)
            C_BLOCKS_MINED.labels(node=f"{NODE_PREFIX}-{miner_idx}").inc()
            push_event("auto_mine", f"auto-mined block on node-{miner_idx}", node=miner_idx)
        except RPCError as e:
            log.debug("generatetoaddress on node-%d: %s", miner_idx, e)


# --------------------------------------------------------------------------- #
# Tx loop                                                                     #
# --------------------------------------------------------------------------- #


def tx_loop() -> None:
    """Auto-tx loop. Like mining_loop, checks STATE.tx_enabled at every
    tick so the UI can pause it cleanly."""
    if not _is_regtest:
        # On non-regtest, bootstrap never runs and we have no source funds
        # — exit so the loop doesn't spin idly polling a flag that can't
        # do anything useful.  Manual /api/tx still works.
        log.info("tx auto-loop not applicable on %s", NETWORK)
        return
    while True:
        with STATE.lock:
            if STATE.bootstrapped:
                break
        time.sleep(2)
    log.info("tx loop starting (interval=%.1fs)", TX_INTERVAL)

    src_idx = 0  # node-0 has the bootstrap funds
    while True:
        time.sleep(TX_INTERVAL)
        if not is_tx_enabled():
            continue
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
# Action helpers (for UI buttons + REST API)                                  #
# --------------------------------------------------------------------------- #


def _wallet_address(node_idx: int) -> str | None:
    """Return a usable address for `node_idx`, creating a wallet if needed."""
    return address_for(node_idx)


def action_mine(node: int, blocks: int) -> dict[str, Any]:
    if not _is_regtest:
        return {"ok": False, "error": "mining is regtest-only"}
    if node < 0 or node >= NODES:
        return {"ok": False, "error": f"node out of range (0..{NODES-1})"}
    blocks = max(1, min(blocks, 500))
    addr = _wallet_address(node)
    if not addr:
        return {"ok": False, "error": "could not derive wallet address"}
    try:
        hashes = rpc_for(node).call(
            "generatetoaddress", blocks, addr, timeout=120.0,
        )
    except RPCError as e:
        return {"ok": False, "error": str(e)}
    C_BLOCKS_MINED.labels(node=f"{NODE_PREFIX}-{node}").inc(blocks)
    push_event("mine", f"mined {blocks} block(s) on node-{node}",
               node=node, blocks=blocks)
    return {"ok": True, "blocks": blocks, "first": hashes[0] if hashes else None,
            "last": hashes[-1] if hashes else None}


def action_tx(src: int, dst: int | None, amount: float) -> dict[str, Any]:
    if not _is_regtest:
        return {"ok": False, "error": "tx generator requires regtest funds"}
    if src < 0 or src >= NODES:
        return {"ok": False, "error": "src out of range"}
    if dst is None:
        dst = (src + 1 + random.randrange(max(1, NODES - 1))) % NODES
        if dst == src and NODES > 1:
            dst = (src + 1) % NODES
    if dst < 0 or dst >= NODES:
        return {"ok": False, "error": "dst out of range"}
    dst_addr = _wallet_address(dst)
    if not dst_addr:
        return {"ok": False, "error": "could not derive dst address"}
    amount = max(0.00000546, min(amount, 1000.0))
    try:
        txid = rpc_for(src).call(
            "sendtoaddress", dst_addr, amount,
            wallet="default", timeout=15.0,
        )
    except RPCError as e:
        C_TX_FAILED.labels(
            src_node=f"{NODE_PREFIX}-{src}", reason=f"rpc_{e.code}"
        ).inc()
        return {"ok": False, "error": str(e)}
    C_TX_SENT.labels(
        src_node=f"{NODE_PREFIX}-{src}",
        dst_node=f"{NODE_PREFIX}-{dst}",
    ).inc()
    push_event("tx", f"sent {amount:.6f} from node-{src} to node-{dst}",
               src=src, dst=dst, amount=amount, txid=txid)
    return {"ok": True, "txid": txid, "src": src, "dst": dst, "amount": amount}


def action_storm(count: int) -> dict[str, Any]:
    count = max(1, min(count, 200))
    sent = 0
    failed = 0
    for _ in range(count):
        src = 0  # bootstrap funds live on node-0
        dst = random.randrange(NODES)
        if dst == src and NODES > 1:
            dst = (src + 1) % NODES
        amount = round(random.uniform(0.00001, 0.005), 8)
        r = action_tx(src, dst, amount)
        if r.get("ok"):
            sent += 1
        else:
            failed += 1
    push_event("storm", f"tx storm: {sent} sent, {failed} failed (asked for {count})",
               sent=sent, failed=failed, requested=count)
    return {"ok": True, "sent": sent, "failed": failed, "requested": count}


def action_isolate(node: int) -> dict[str, Any]:
    """Disconnect node from every other peer by toggling networkactive=false.
    All open connections drop and no new ones are dialled until /api/heal."""
    if node < 0 or node >= NODES:
        return {"ok": False, "error": "node out of range"}
    try:
        rpc_for(node).call("setnetworkactive", False)
    except RPCError as e:
        return {"ok": False, "error": str(e)}
    with STATE.lock:
        STATE.isolated.add(node)
    push_event("isolate", f"isolated node-{node} (networkactive=false)", node=node)
    return {"ok": True, "node": node}


def action_heal(node: int) -> dict[str, Any]:
    if node < 0 or node >= NODES:
        return {"ok": False, "error": "node out of range"}
    try:
        rpc_for(node).call("setnetworkactive", True)
    except RPCError as e:
        return {"ok": False, "error": str(e)}
    # Kick the addnode list so it actually re-dials the mesh quickly.
    for j in range(NODES):
        if j == node:
            continue
        try:
            rpc_for(node).call(
                "addnode",
                f"{NODE_PREFIX}-{j}.{HEADLESS}:{18444 if NETWORK == 'regtest' else 18333}",
                "onetry",
                timeout=5.0,
            )
        except RPCError:
            pass
    with STATE.lock:
        STATE.isolated.discard(node)
    push_event("heal", f"healed node-{node} (networkactive=true)", node=node)
    return {"ok": True, "node": node}


def action_auto(mining: bool | None, tx: bool | None) -> dict[str, Any]:
    with STATE.lock:
        if mining is not None:
            STATE.mining_enabled = bool(mining)
        if tx is not None:
            STATE.tx_enabled = bool(tx)
        cur = {
            "mining": is_mining_enabled() if STATE.mining_enabled is not None or ENABLE_MINING else False,
            "tx": is_tx_enabled() if STATE.tx_enabled is not None or ENABLE_TX else False,
        }
    push_event("auto", f"auto loops set to mining={cur['mining']} tx={cur['tx']}", **cur)
    return {"ok": True, **cur}


def action_pace(block_interval: float | None, tx_interval: float | None) -> dict[str, Any]:
    global BLOCK_INTERVAL, TX_INTERVAL
    if block_interval is not None:
        BLOCK_INTERVAL = max(0.5, min(float(block_interval), 600.0))
    if tx_interval is not None:
        TX_INTERVAL = max(0.1, min(float(tx_interval), 60.0))
    push_event("pace",
               f"intervals updated: block={BLOCK_INTERVAL:.1f}s tx={TX_INTERVAL:.2f}s",
               block_interval=BLOCK_INTERVAL, tx_interval=TX_INTERVAL)
    return {"ok": True, "block_interval": BLOCK_INTERVAL, "tx_interval": TX_INTERVAL}


def build_full_state() -> dict[str, Any]:
    with STATE.lock:
        snaps = list(STATE.snapshots.values())
        cluster = dict(STATE.cluster)
        events = list(STATE.events)[:50]
        isolated = sorted(STATE.isolated)
        mining = is_mining_enabled()
        tx_on = is_tx_enabled()
    nodes = []
    for i in range(NODES):
        snap = next((s for s in snaps if s["idx"] == i), None) or {
            "idx": i, "label": f"{NODE_PREFIX}-{i}", "up": False,
        }
        snap = dict(snap)
        snap["isolated"] = i in isolated
        nodes.append(snap)
    return {
        "network": NETWORK,
        "regtest": _is_regtest,
        "cluster": cluster,
        "nodes": nodes,
        "events": events,
        "auto": {
            "mining": mining,
            "tx": tx_on,
            "block_interval": BLOCK_INTERVAL,
            "tx_interval": TX_INTERVAL,
        },
    }


# --------------------------------------------------------------------------- #
# UI / REST server                                                            #
# --------------------------------------------------------------------------- #


class UIHandler(BaseHTTPRequestHandler):
    server_version = "btqnet-controller/1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Quieter than the default handler — go through `log` so we
        # don't spam stderr at INFO every poll.
        log.debug("ui %s - %s", self.address_string(), format % args)

    # --------- helpers ---------
    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # --------- routing ---------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            except FileNotFoundError:
                self._send_html(500, "index.html missing in container")
                return
            self._send_html(200, html)
            return
        if path == "/api/state":
            self._send_json(200, build_full_state())
            return
        if path == "/api/health":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()
        # Actions whose effects are visible in the next scrape pass.
        # We re-scrape immediately afterwards so the UI doesn't have
        # to wait up to SCRAPE_INTERVAL seconds for the change to show.
        rescrape_after = {"/api/mine", "/api/tx", "/api/storm",
                          "/api/isolate", "/api/heal"}
        try:
            if path == "/api/mine":
                r = action_mine(int(body.get("node", 0)),
                                int(body.get("blocks", 1)))
            elif path == "/api/tx":
                dst = body.get("dst")
                r = action_tx(int(body.get("src", 0)),
                              int(dst) if dst is not None else None,
                              float(body.get("amount", 0.001)))
            elif path == "/api/storm":
                r = action_storm(int(body.get("count", 10)))
            elif path == "/api/isolate":
                r = action_isolate(int(body.get("node", 0)))
            elif path == "/api/heal":
                r = action_heal(int(body.get("node", 0)))
            elif path == "/api/auto":
                r = action_auto(body.get("mining"), body.get("tx"))
            elif path == "/api/pace":
                r = action_pace(body.get("block_interval"), body.get("tx_interval"))
            else:
                self._send_json(404, {"error": "not found"})
                return
        except Exception as e:  # pragma: no cover - never crash the UI
            log.exception("action failed")
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        if path in rescrape_after and r.get("ok"):
            try:
                scrape_once()
            except Exception:  # pragma: no cover
                log.exception("post-action scrape failed")
        self._send_json(200 if r.get("ok") else 400, r)


def ui_loop() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", UI_PORT), UIHandler)
    log.info("ui server listening on :%d", UI_PORT)
    server.serve_forever()


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #


def main() -> None:
    log.info(
        "btqnet controller starting :: nodes=%d network=%s headless=%s "
        "rpc_port=%d scrape=%.1fs mining=%s tx=%s bootstrap=%s",
        NODES, NETWORK, HEADLESS, RPC_PORT, SCRAPE_INTERVAL,
        ENABLE_MINING, ENABLE_TX, ENABLE_BOOTSTRAP,
    )
    M_CLUSTER_NODES_TOTAL.set(NODES)
    start_http_server(METRICS_PORT, registry=REG)
    log.info("prometheus exporter listening on :%d/metrics", METRICS_PORT)
    threading.Thread(target=scrape_loop, daemon=True, name="scrape").start()
    threading.Thread(target=mining_loop, daemon=True, name="mining").start()
    threading.Thread(target=tx_loop, daemon=True, name="tx").start()
    threading.Thread(target=ui_loop, daemon=True, name="ui").start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
