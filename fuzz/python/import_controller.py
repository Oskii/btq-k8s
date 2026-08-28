#!/usr/bin/env python3
"""Reproduce controller.py import-time env parsing. Exit 2 = uncaught crash."""
from __future__ import annotations

import os
import sys

KEY = sys.argv[1] if len(sys.argv) > 1 else "BTQ_NODES"
VAL = sys.argv[2] if len(sys.argv) > 2 else "x"

env = {
    "BTQ_NODES": "2",
    "BTQ_RPC_PORT": "18443",
    "SCRAPE_INTERVAL": "3",
    "BLOCK_INTERVAL": "10",
    "TX_INTERVAL": "4",
    "BOOTSTRAP_BLOCKS": "101",
    "METRICS_PORT": "9100",
    "UI_PORT": "9200",
    "BTQ_NETWORK": "regtest",
}
env[KEY] = VAL
os.environ.update(env)

try:
    # Mirrors controller.py lines 54-76
    NODES = int(os.environ.get("BTQ_NODES", "5"))
    NETWORK = os.environ.get("BTQ_NETWORK", "regtest")
    _DEFAULT_RPC_PORT = {
        "regtest": 18443,
        "test": 18332,
        "signet": 38332,
        "main": 8332,
    }
    RPC_PORT = int(os.environ.get("BTQ_RPC_PORT") or _DEFAULT_RPC_PORT.get(NETWORK, 18443))
    SCRAPE_INTERVAL = float(os.environ.get("SCRAPE_INTERVAL", "3"))
    BLOCK_INTERVAL = float(os.environ.get("BLOCK_INTERVAL", "10"))
    TX_INTERVAL = float(os.environ.get("TX_INTERVAL", "4"))
    BOOTSTRAP_BLOCKS = int(os.environ.get("BOOTSTRAP_BLOCKS", "101"))
    METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))
    UI_PORT = int(os.environ.get("UI_PORT", "9200"))
except (ValueError, OverflowError) as e:
    print(f"UNCAUGHT {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(2)

print("ok")
