#!/usr/bin/env python3
"""Fuzz action_* clamp / coerce logic without importing controller.py."""
from __future__ import annotations

import json
import sys


NODES = 5


def clamp_mine(body: object) -> dict:
    if not isinstance(body, dict):
        return {"ok": False, "error": "not object"}
    try:
        node = int(body.get("node", 0))
        blocks = int(body.get("blocks", 1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad types"}
    if node < 0 or node >= NODES:
        return {"ok": False, "error": "node out of range"}
    blocks = max(1, min(blocks, 500))
    return {"ok": True, "node": node, "blocks": blocks}


def clamp_tx(body: object) -> dict:
    if not isinstance(body, dict):
        return {"ok": False, "error": "not object"}
    try:
        src = int(body.get("src", 0))
        dst = body.get("dst")
        dst_i = int(dst) if dst is not None else 1
        amount = float(body.get("amount", 0.001))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad types"}
    if src < 0 or src >= NODES or dst_i < 0 or dst_i >= NODES:
        return {"ok": False, "error": "range"}
    amount = max(0.00000546, min(amount, 1000.0))
    return {"ok": True, "src": src, "dst": dst_i, "amount": amount}


def clamp_storm(body: object) -> dict:
    if not isinstance(body, dict):
        return {"ok": False, "error": "not object"}
    try:
        count = int(body.get("count", 1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad types"}
    count = max(1, min(count, 200))
    return {"ok": True, "count": count}


def clamp_pace(body: object) -> dict:
    if not isinstance(body, dict):
        return {"ok": False, "error": "not object"}
    bi = body.get("block_interval")
    ti = body.get("tx_interval")
    out = {}
    if bi is not None:
        out["block_interval"] = max(0.5, min(float(bi), 600.0))
    if ti is not None:
        out["tx_interval"] = max(0.1, min(float(ti), 60.0))
    return {"ok": True, **out}


def read_json(raw: bytes, content_length: int | None) -> dict:
    if content_length is not None and content_length > 1_000_000:
        raise ValueError("unbounded Content-Length")
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", errors="replace"))


def main() -> None:
    raw = sys.stdin.buffer.read()
    try:
        body = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
    except Exception:
        body = {}
    results = [
        clamp_mine(body),
        clamp_tx(body),
        clamp_storm(body),
    ]
    try:
        results.append(clamp_pace(body))
    except (TypeError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"pace: {e}"}))
        sys.exit(2)
    for r in results:
        if "blocks" in r and r.get("ok"):
            assert 1 <= r["blocks"] <= 500
        if "count" in r and r.get("ok"):
            assert 1 <= r["count"] <= 200
        if "amount" in r and r.get("ok"):
            assert 0.00000546 <= r["amount"] <= 1000.0
    print(json.dumps({"ok": True, "results": results}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        sys.exit(2)
