#!/usr/bin/env python3
"""Walk scrape_node's field access against hostile RPC JSON. Exit 2 on crash."""
from __future__ import annotations

import json
import sys
from typing import Any


def scrape_node_pure(rpc: dict[str, Any]) -> dict[str, Any] | None:
    net = rpc.get("getnetworkinfo")
    chain = rpc.get("getblockchaininfo")
    peers = rpc.get("getpeerinfo")
    mempool = rpc.get("getmempoolinfo")
    nettotals = rpc.get("getnettotals")
    chaintips = rpc.get("getchaintips")
    uptime = rpc.get("uptime")

    missing = [k for k in (
        "getnetworkinfo", "getblockchaininfo", "getpeerinfo",
        "getmempoolinfo", "getnettotals", "getchaintips",
    ) if k not in rpc]
    if missing:
        return None

    out: dict[str, Any] = {"up": True}
    out["version"] = str(net.get("version", "")) if isinstance(net, dict) else ""
    if not isinstance(peers, list):
        raise TypeError("peers not list")
    inbound = sum(1 for p in peers if isinstance(p, dict) and p.get("inbound"))
    out["peers"] = len(peers)
    out["peers_in"] = inbound
    if not isinstance(chaintips, list):
        raise TypeError("chaintips not list")
    tip_buckets: dict[str, int] = {}
    for tip in chaintips:
        if not isinstance(tip, dict):
            raise TypeError("tip not dict")
        tip_buckets[tip["status"]] = tip_buckets.get(tip["status"], 0) + 1
    for tip in chaintips:
        if tip["status"] == "active":
            out["best_hash"] = tip["hash"]
            out["best_height"] = tip["height"]
            break
    if isinstance(chain, dict):
        out["blocks"] = chain.get("blocks", 0)
    if isinstance(mempool, dict):
        out["mempool_tx"] = mempool.get("size", 0)
    out["uptime"] = uptime or 0
    return out


def main() -> None:
    raw = sys.stdin.buffer.read()
    try:
        rpc = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except Exception:
        print(json.dumps({"ok": True, "skipped": "invalid json"}))
        return
    if not isinstance(rpc, dict):
        rpc = {"payload": rpc}
    try:
        scrape_node_pure(rpc)
    except (KeyError, TypeError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        sys.exit(2)
    print(json.dumps({"ok": True}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        sys.exit(2)
