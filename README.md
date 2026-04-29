# btq-k8s — local Kubernetes lab for BTQ Core

Spin up a fully-meshed network of up to 20 `btqd` nodes on a local
Kubernetes cluster (kind) and observe everything they do — block heights,
peer connections, mempool propagation, fork detection, byte counters, and
raw debug logs — through a pre-provisioned Grafana dashboard backed by
Prometheus + Loki.

The whole thing lives in this directory and is driven by a single CLI:
[`bin/btqnet`](bin/btqnet).

```
$ bin/btqnet up 5             # 5 BTQ nodes on regtest + observability stack
$ bin/btqnet up 5 testnet     # 5 BTQ nodes on the real BTQ testnet
$ bin/btqnet up testnet 10    # (positional args may come in either order)
$ bin/btqnet scale 20         # grow to 20 nodes, no data loss
$ bin/btqnet down             # delete the kind cluster, free resources
```

`bin/btqnet up` accepts an optional second arg specifying the chain:
`regtest` (default), `testnet` / `test`, `signet`, or `main` /
`mainnet`. The choice is persisted in `.rendered/network` so subsequent
`scale`, `cli`, `rpc`, `peers` etc. commands automatically use the
right `-chain=...` flag and probe the right ports.

---

## What you get

| component | image | what it does |
|---|---|---|
| **btq-node** (StatefulSet, 1..20 replicas) | `btq-node:dev` | A real `btqd` from `/home/o/BTQ/btq-core/release/linux-x86_64/`, running in regtest with full RPC/ZMQ exposure. Every pod auto-discovers its peers via the headless service and `-addnode`s every other node, producing a true full-mesh P2P network. |
| **btq-controller** (Deployment) | `btq-controller:dev` | A small Python service that (a) scrapes every node's RPC every 3 s and re-publishes ~25 Prometheus metrics, (b) generates blocks on a rotating node every 10 s after a 101-block bootstrap, (c) sends random transactions every 4 s so the mempool is never empty, **and (d) serves an interactive console UI at http://localhost:30200 with buttons to mine, send tx, partition / heal nodes, run tx storms, and toggle the auto loops.** |
| **Prometheus** | `prom/prometheus:v3.1.0` | Scrapes the controller plus any pod with `prometheus.io/scrape: true`. |
| **Grafana** | `grafana/grafana:11.4.0` | Pre-provisioned with the BTQnet dashboard as the default home. Reachable at `http://localhost:30030` (admin / btqnet). |
| **Loki** | `grafana/loki:3.3.2` | Single-binary monolithic. Holds all `btqd` debug logs for ad-hoc querying. |
| **Promtail** (DaemonSet) | `grafana/promtail:3.3.2` | Tails every pod log in the `btqnet` namespace and ships it to Loki. |

All of it runs inside a single-node [kind](https://kind.sigs.k8s.io/)
cluster called `btqnet`, with NodePorts mapped to host ports so you can
reach Grafana / Prometheus / Loki / the raw exporter without any
`kubectl port-forward` plumbing.

---

## Topology

```
                ┌──────────────────────┐                ┌─────────────────────┐
                │  Grafana :30030      │  PromQL/LogQL  │ Prometheus :30090   │
                │  (BTQnet dashboard)  │◀──────────────▶│ + Loki :30310       │
                └─────────▲────────────┘                └──────────▲──────────┘
                          │                                        │
                          │ /metrics                               │ /loki/api/...
                          │                                        │
                ┌─────────┴────────────┐                ┌──────────┴──────────┐
                │ btq-controller :9100 │  RPC scrape    │ promtail (DaemonSet)│
                │  (exporter + miner   │◀──────────────▶│  tails /var/log/pods│
                │   + tx generator)    │   18443        └─────────────────────┘
                └─────────▲────────────┘
                          │ JSON-RPC
            ┌─────────────┼─────────────┬─────────────┬─────────────┐
            │             │             │             │             │
       ┌────┴────┐   ┌────┴────┐   ┌────┴────┐   ┌────┴────┐   ┌────┴────┐
       │btq-node │◀─▶│btq-node │◀─▶│btq-node │◀─▶│  ...    │◀─▶│btq-node │
       │   -0    │   │   -1    │   │   -2    │   │         │   │   -N    │
       └─────────┘   └─────────┘   └─────────┘   └─────────┘   └─────────┘
        18443/RPC    full-mesh P2P over 18444    +ZMQ on 28332 (block/tx) and 28333 (sequence)
```

Inside the cluster every node is reachable via the headless service:

```
btq-node-3.btq-headless.btqnet.svc.cluster.local:18444   # P2P
btq-node-3.btq-headless.btqnet.svc.cluster.local:18443   # RPC
btq-node-3.btq-headless.btqnet.svc.cluster.local:28332   # ZMQ raw block/tx + hashes
btq-node-3.btq-headless.btqnet.svc.cluster.local:28333   # ZMQ sequence
```

Every pod's entrypoint reads the StatefulSet ordinal out of `$HOSTNAME`,
then constructs `-addnode=btq-node-X.btq-headless:18444` for every
peer X != self. The result is a deterministic, complete P2P graph — no
DNS seeding, no UPnP, no Tor.

---

## Quickstart

Requirements: Docker (with the WSL Linux daemon, not Docker-Desktop's
credstore), enough free RAM to run N nodes (each is ~256 MiB request,
1 GiB limit), and either `~/.local/bin` on PATH or a willingness to use
`/home/o/.local/bin/...` directly.

```bash
cd /home/o/BTQ/btq-k8s
./bin/btqnet up 5
```

This builds the two images (`btq-node:dev`, `btq-controller:dev`) if
they're missing, creates the kind cluster, loads the images, applies all
manifests, and waits for everything to be ready. It ends by printing:

```
  Console UI : http://localhost:30200      ← buttons / sim controls
  Grafana    : http://localhost:30030      (admin / btqnet)
  Prometheus : http://localhost:30090
  Metrics    : http://localhost:30100/metrics
  Loki       : http://localhost:30310
```

The default Grafana home dashboard is `BTQnet — Cluster Overview`.

---

## Console UI

`http://localhost:30200` is a single-page web console served by the
controller pod. Open it next to Grafana and use the buttons to *cause*
network events while you watch them propagate.

It always shows the current state of the cluster:

- **Top bar**: network name, reachable / total nodes, max-height,
  height-spread, distinct-best-block count (turns red on a fork), live
  toggle status of `auto-mine` / `auto-tx`.
- **Node grid** (one card per pod): height, peer count, mempool depth,
  uptime, best-block hash. Cards are coloured by the *fork* their best
  block belongs to — when a partition forks the cluster, you can see
  it instantly because some cards switch colour. Isolated nodes get a
  red `ISO` badge.
- **Action panel**:
  - **Mine blocks** — pick a node, set count, hit Mine (or use the
    `×1 / ×5 / ×25 / ×101` quick buttons).
  - **Send transaction** — `from` → `to` with an explicit amount, or
    fire a **Tx storm** of N random spends.
  - **Network partition / fork** — `Isolate` flips
    `setnetworkactive=false` on a node (every connection drops, no
    new ones are dialled). `Heal` flips it back on and `addnode
    onetry`s every peer to reconnect immediately.
  - **Auto loops** — pause/resume the controller's mining and tx
    threads; tweak `BLOCK_INTERVAL` and `TX_INTERVAL` live.
- **Event log** (right side): scrolling, colour-coded record of every
  action you and the auto loops have taken.

### The fork demo, all from buttons

1. Click **auto-mine OFF** so nothing else is producing blocks.
2. In the partition row, pick `node-2`, click **Isolate** — its card
   turns red with an `ISO` badge.
3. Click `Mine ×5` on node-2's card — node-2's chain advances, but
   only locally.
4. Click `Mine ×3` on node-0's card — the rest of the cluster moves
   forward on a different chain.
5. The node cards split into two colours, the top bar's "distinct
   tips" pill jumps to **2** (red), and `spread` ticks up.
6. Click **Heal** on node-2 — it reconnects, downloads the longer
   chain (node-2's own 5-block branch), reorganises, and within a
   couple of seconds every card converges back to one colour and
   `spread = 0`.

This is the same flow described in the recipes section below, but
without ever leaving the browser.

---

## CLI

```
bin/btqnet up [N] [NETWORK]           # 1 ≤ N ≤ 20 (default 5), NETWORK ∈
                                      # {regtest, testnet, signet, main} (default regtest)
bin/btqnet down                       # delete the kind cluster
bin/btqnet scale N                    # rolling scale to N nodes (network unchanged)
bin/btqnet chain                      # print the network the cluster is on
bin/btqnet status                     # kubectl get pods + node count
bin/btqnet build                      # rebuild images, hot-reload pods
bin/btqnet cli IDX -- ARGS ...        # run btq-cli on node IDX
bin/btqnet rpc IDX METHOD [PARAMS...] # arbitrary RPC call
bin/btqnet mine [IDX] [N]             # generate N blocks on node IDX (regtest only)
bin/btqnet peers IDX                  # pretty-print getpeerinfo
bin/btqnet logs IDX|controller|...    # follow a pod's logs
bin/btqnet metrics                    # curl the controller exporter
bin/btqnet dash                       # print URLs again
```

Examples:

```bash
bin/btqnet cli 3 -- getblockchaininfo | jq .blocks
bin/btqnet rpc 0 getmempoolinfo
bin/btqnet mine 7 5         # mine 5 blocks on node-7
bin/btqnet peers 0          # see which nodes node-0 is connected to
bin/btqnet logs 2           # tail node-2's btqd debug log
bin/btqnet logs controller  # tail the controller (mining/tx events)
```

---

## Dashboard panels

The Grafana dashboard is split into:

1. **Cluster summary row (stat panels)** — reachable / total nodes,
   max height, height-spread, distinct best-block hashes (>1 means
   the cluster is forked right now), blocks/sec, tx/sec.
2. **Block height per node** + **peer count per node** time series.
3. **Block-propagation latency** — for every block hash, the controller
   records the wall-clock between *first* node to see it and *each*
   subsequent node. The bar chart shows the most recent value per
   node, so you can literally watch propagation get slower as the
   cluster grows or as you induce delays.
4. **Mempool size per node** — see transactions land on node-0 and
   ripple out to the rest.
5. **P2P bytes/sec recv & sent** — split by node.
6. **Chain-tips table** — counts of `active` / `valid-fork` /
   `valid-headers` / `headers-only` / `invalid` per node. Anything
   non-zero in the last four columns means a fork was observed.
7. **Blocks mined per node** — confirms the controller's rotation is
   actually exercising every miner.
8. **Loki logs** — live tail of every `btqd` pod, filterable by
   `{namespace="btqnet", app_kubernetes_io_name="btq-node"}`.

Variables:

- `$node` (multi-select) — drives every panel; pick a subset of nodes
  to focus on.

---

## Running on testnet

```bash
bin/btqnet up 5 testnet
```

What's different vs regtest:

| concern | regtest | testnet |
|---|---|---|
| Mining | controller mines 1 block / 10 s, rotating across nodes; bootstrap mines 101 blocks for coinbase maturity. | controller does **not** mine — real PoW + real difficulty mean a single CPU pod has zero chance. The mining loop is auto-disabled. |
| TX generation | controller spends from node-0's bootstrap wallet. | also disabled — testnet wallets need to be funded externally (a faucet, or import a known testnet privkey). |
| Bootstrap | 101 blocks mined to node-0. | skipped. Each pod kicks off Initial Block Download instead. |
| DNS seeds | disabled (`-dnsseed=0`). | **enabled**. Each node resolves `testnet-seed1.bitcoinquantum.com` / `testnet-seed2.bitcoinquantum.com` and pulls real testnet peers, on top of the in-cluster mesh. |
| Pruning | off — full chain (~2 GB max in regtest). | `-prune=550` MiB by default to bound disk usage. `-txindex` is disabled in this mode (txindex is incompatible with pruning). |
| `-maxconnections` | 125 (Bitcoin default). | 32, so a 20-node cluster doesn't pull 2 500 inbound connections from public peers. |
| Storage | 2 GiB PVC per pod. | 10 GiB PVC per pod. |
| Memory limit | 1 GiB per pod. | 2 GiB per pod (IBD + chainstate are heavier). |
| RPC port | 18443. | 18332. |
| P2P port | 18444. | 18333. |

After `up`, watch IBD progress per node:

```bash
for i in 0 1 2 3 4; do
  printf "node-%d  " $i
  bin/btqnet cli $i -- getblockchaininfo \
    | jq -r '"blocks=\(.blocks)  headers=\(.headers)  vp=\(.verificationprogress)"'
done
```

Or, in Grafana, the `btq_node_blocks` panel will show each node's
height climbing toward `btq_node_headers`. The
`btq_cluster_distinct_best_blocks` and `btq_cluster_height_spread`
gauges naturally start out > 1 / > 0 during IBD and converge to 1 / 0
once every node is fully synced. That's the cluster reaching consensus
visualised in real time.

### When you actually want to mine on testnet

Either point a real miner at one of the cluster's nodes (e.g. cgminer
`-o stratum+tcp://...` against a stratum proxy you run alongside), or
enable opportunistic CPU-mining inside the controller:

```bash
kubectl --context kind-btqnet -n btqnet \
  set env deploy/btq-controller ENABLE_MINING=1 BLOCK_INTERVAL=300
```

This calls `generatetoaddress` exactly as on regtest. On a chain whose
testnet difficulty has dropped to the min-diff floor (which BTQ
testnet currently sits on), the call returns within seconds; on
higher-difficulty chains it will simply time out and the loop will
keep retrying. Set it back to `0` to stop.

### Funding wallets on testnet

The controller does not auto-fund anything. To send transactions on
testnet:

```bash
# import an existing privkey
bin/btqnet rpc 0 importprivkey "<WIF>"

# or get an address and request from a faucet
bin/btqnet rpc 0 -rpcwallet=default getnewaddress
```

Then `ENABLE_TX=1 BTQ_RPC_USER=... ...` on the controller will start
the tx loop again.

---

## Useful experiments

The whole point of running this many nodes is to *do things to them*.
A few quick recipes that work out of the box:

### Watch a block propagate

```bash
bin/btqnet mine 4 1
```

…then look at `btq_block_propagation_last_seconds` on the dashboard.
You should see the propagation panel light up once for every node that
isn't `btq-node-4`, with the wall-clock from when node-4 broadcast the
block.

### Force a fork and watch it resolve

Disconnect node-3 from everyone except node-4, mine on both halves,
reconnect:

```bash
bin/btqnet rpc 3 disconnectnode "btq-node-0.btq-headless:18444"
bin/btqnet rpc 3 disconnectnode "btq-node-1.btq-headless:18444"
bin/btqnet rpc 3 disconnectnode "btq-node-2.btq-headless:18444"
# leave node-3 ↔ node-4 alone
bin/btqnet mine 0 3   # majority side
bin/btqnet mine 3 1   # minority side
# Now btq_cluster_distinct_best_blocks == 2 in Grafana.
# Reconnect:
bin/btqnet rpc 3 addnode "btq-node-0.btq-headless:18444" "onetry"
# Expect distinct_best_blocks → 1, node-3 chain-tips table grows a
# `valid-fork` row, then garbage-collects to 0.
```

### Inject a tx storm

```bash
# crank tx generation to one tx per 200 ms
kubectl --context kind-btqnet -n btqnet set env deploy/btq-controller TX_INTERVAL=0.2
```

Watch `btq_node_mempool_tx{node="$node"}` climb on every node. When the
next block arrives the panel drops in lockstep — this is mempool
synchronisation in action.

### Slow a node down

You can simulate a struggling peer by setting CPU limits:

```bash
kubectl --context kind-btqnet -n btqnet patch statefulset btq-node \
  --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/cpu","value":"100m"}]'
kubectl --context kind-btqnet -n btqnet rollout restart statefulset/btq-node
```

`btq_block_propagation_last_seconds` for the throttled nodes will spike.

---

## Layout

```
btq-k8s/
├── bin/btqnet                 ← single-entrypoint CLI (bash)
├── docker/
│   ├── node/                  ← btqd container
│   │   ├── Dockerfile
│   │   ├── entrypoint.sh      ← parses ordinal from $HOSTNAME, builds -addnode list
│   │   ├── btqd               ← stripped binary copied from btq-core/release/linux-x86_64
│   │   └── btq-cli
│   └── controller/            ← analytics + traffic generator + UI/REST
│       ├── Dockerfile
│       ├── controller.py      ← scrape loop + mining loop + tx loop + UI server
│       ├── static/index.html  ← single-page console served at :30200
│       └── requirements.txt
├── k8s/                       ← raw manifests
│   ├── kind-cluster.yaml      ← kind cluster + extraPortMappings (30030/30090/30100/30310)
│   ├── 00-namespace.yaml
│   ├── 10-btq-services.yaml   ← headless + RPC ClusterIP services
│   ├── 20-btq-statefulset.yaml← __BTQ_NODES__ replicas, ConfigMap, Secret
│   ├── 30-controller.yaml     ← Deployment + NodePort exporter
│   ├── 40-prometheus.yaml     ← + RBAC + ConfigMap
│   ├── 50-loki.yaml           ← + Promtail DaemonSet + RBAC
│   └── 60-grafana.yaml        ← + datasource provisioning
├── grafana/
│   ├── dashboards/btqnet.json ← shipped via ConfigMap created from this file
│   └── provisioning/...       ← reference copies; the live ones are inline in 60-grafana.yaml
└── .rendered/                 ← bin/btqnet writes its rendered manifest here
```

`__BTQ_NODES__` is the only template placeholder. `bin/btqnet` runs
`sed "s/__BTQ_NODES__/$N/g"` over each manifest at apply time — no Helm
or kustomize required.

---

## Notes & caveats

- **Storage is per-pod `PersistentVolumeClaim`** (kind ships the
  `standard` storage class, backed by local-path-provisioner). Wallets
  and chain data survive pod restarts and rolling updates. They do
  *not* survive `bin/btqnet down`, which deletes the entire kind
  cluster. If you scale down (e.g. `scale 5` after running with 10),
  the orphaned PVCs (`data-btq-node-5` … `-9`) stick around and will
  be re-attached if you scale back up — handy for keeping IBD
  progress on testnet.
- **Block reward is 5 BTQ on regtest** in this build of BTQ Core (not
  the 50 BTQ Bitcoin pays out). 101 mature coinbases = 505 BTQ on
  node-0, which the controller spends from for its tx loop.
- **`enableServiceLinks: false`** is set on every pod spec, otherwise
  Kubernetes auto-injects environment variables like
  `BTQ_RPC_PORT=tcp://10.96.x.x:18443` from the `btq-rpc` service and
  poisons the controller's integer parsing. (Found this the fun way.)
- **Docker Desktop credstore on WSL** breaks `docker build` if your
  `~/.docker/config.json` references `desktop.exe`. The setup process
  rewrote it to `{}` once. If you reinstall Docker Desktop and it
  comes back, just `cp ~/.docker/config.json.bak.btqsetup
  ~/.docker/config.json` (or echo `{}` again).
- **Maximum supported size** is 20 because that's where the project
  brief drew the line. The controller, dashboard, and CLI will all
  happily go higher; only the `bin/btqnet up` / `scale` validators
  refuse it.
