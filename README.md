# btq-k8s — local Kubernetes lab for BTQ Core

Spin up a fully-meshed network of up to 20 `btqd` nodes on a local
Kubernetes cluster (kind) and observe everything they do — block heights,
peer connections, mempool propagation, fork detection, byte counters, and
raw debug logs — through a pre-provisioned Grafana dashboard backed by
Prometheus + Loki.

The whole thing lives in this directory and is driven by a single CLI:
[`bin/btqnet`](bin/btqnet).

```
$ bin/btqnet up 5      # 5 BTQ nodes + observability stack
$ bin/btqnet scale 20  # grow to 20 nodes, no data loss
$ bin/btqnet down      # delete the kind cluster, free resources
```

---

## What you get

| component | image | what it does |
|---|---|---|
| **btq-node** (StatefulSet, 1..20 replicas) | `btq-node:dev` | A real `btqd` from `/home/o/BTQ/btq-core/release/linux-x86_64/`, running in regtest with full RPC/ZMQ exposure. Every pod auto-discovers its peers via the headless service and `-addnode`s every other node, producing a true full-mesh P2P network. |
| **btq-controller** (Deployment) | `btq-controller:dev` | A small Python service that (a) scrapes every node's RPC every 3 s and re-publishes ~25 Prometheus metrics, (b) generates blocks on a rotating node every 10 s after a 101-block bootstrap, (c) sends random transactions every 4 s so the mempool is never empty. |
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
  Grafana     :  http://localhost:30030      (admin / btqnet)
  Prometheus  :  http://localhost:30090
  Controller  :  http://localhost:30100/metrics
  Loki        :  http://localhost:30310
```

The default home dashboard is `BTQnet — Cluster Overview`.

---

## CLI

```
bin/btqnet up [N]                     # 1 ≤ N ≤ 20, default 5
bin/btqnet down                       # delete the kind cluster
bin/btqnet scale N                    # rolling scale to N nodes
bin/btqnet status                     # kubectl get pods + node count
bin/btqnet build                      # rebuild images, hot-reload pods
bin/btqnet cli IDX -- ARGS ...        # run btq-cli on node IDX
bin/btqnet rpc IDX METHOD [PARAMS...] # arbitrary RPC call
bin/btqnet mine [IDX] [N]             # generate N blocks on node IDX
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
│   └── controller/            ← analytics + traffic generator
│       ├── Dockerfile
│       ├── controller.py      ← scrape loop + mining loop + tx loop in one process
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

- **Storage is `emptyDir`.** Pods that get rescheduled lose their chain
  data and wallets. The controller's bootstrap step is idempotent on
  *wallet balance*, so it will quietly re-mine 101 blocks if node-0's
  wallet ever shows up empty. If you want to do long-running
  experiments where state must survive `kind delete` or pod
  rescheduling, change the `emptyDir` block in
  `k8s/20-btq-statefulset.yaml` to a `volumeClaimTemplates:` entry.
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
