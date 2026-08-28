import { finishedWithin, invariant } from '../engine/assert.js';
import type { FuzzTarget } from '../types.js';

const ALLOWED_NETWORKS = new Set(['regtest', 'test', 'signet', 'main']);
const HEADLESS = 'btq-headless';
const QUANTITY = /^\d+(\.\d+)?(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?$/;
const DNS1123_LABEL = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const XSS_RE = /<|javascript:/i;
const RESOURCE_UNIT = /(Ki|Mi|Gi|Ti|Pi|Ei|[kMGTPE])/;

function asPrintable(buf: Buffer, max = 32): string {
  return buf.toString('utf8').replace(/[^\x20-\x7e]/g, '').slice(0, max);
}

function isDns1123Label(s: string): boolean {
  return s.length >= 1 && s.length <= 63 && DNS1123_LABEL.test(s);
}

/** `pod_for_node` — no validation of idx. */
function podForNode(idx: string): string {
  return `btq-node-${idx}`;
}

/** `node_dns(i)` — f-string, no bounds or DNS check. */
function nodeDns(i: string): string {
  return `btq-node-${i}.${HEADLESS}`;
}

/** `action_heal` P2P port: only special-cases regtest. */
function healP2P(network: string): number {
  return network === 'regtest' ? 18444 : 18333;
}

function expectedP2P(network: string): number | undefined {
  switch (network) {
    case 'regtest':
      return 18444;
    case 'test':
    case 'testnet':
      return 18333;
    case 'signet':
      return 38333;
    case 'main':
    case 'mainnet':
      return 8333;
    default:
      return undefined;
  }
}

/** Python `int(header or 0)` — empty/missing → 0; junk → throw. */
function pythonIntOrZero(raw: string): { ok: true; value: bigint } | { ok: false } {
  if (!raw) return { ok: true, value: 0n };
  const t = raw.trim();
  if (!/^[+-]?\d+$/.test(t)) return { ok: false };
  try {
    return { ok: true, value: BigInt(t) };
  } catch {
    return { ok: false };
  }
}

function looksLikeResource(s: string): boolean {
  return /\d/.test(s) && RESOURCE_UNIT.test(s);
}

/** `cmd_scale` acceptor: integer string in 1..20 only. */
function scaleAccepts(n: string): boolean {
  if (!/^\d+$/.test(n)) return false;
  const v = Number(n);
  return Number.isFinite(v) && v >= 1 && v <= 20;
}

function xssField(s: string): boolean {
  return XSS_RE.test(s);
}

function splitEventFields(input: Buffer): { kind: string; msg: string; best_hash: string } {
  const text = input.toString('utf8');
  try {
    const o = JSON.parse(text) as Record<string, unknown>;
    if (o && typeof o === 'object' && !Array.isArray(o)) {
      return {
        kind: String(o.kind ?? ''),
        msg: String(o.msg ?? ''),
        best_hash: String(o.best_hash ?? o.bestHash ?? ''),
      };
    }
  } catch {
    /* fall through */
  }
  const parts = text.split(/\n|\0/);
  return {
    kind: parts[0] ?? '',
    msg: parts[1] ?? text,
    best_hash: parts[2] ?? '',
  };
}

const extra: FuzzTarget[] = [
  {
    name: 'k8s.sedSpecials',
    project: 'k8s',
    description:
      'sed s/__BTQ_*__/${val}/g — / breaks the delimiter; & is the match metacharacter',
    seeds: ['sed-specials.txt', 'sed-amp.txt', 'nodes.txt'],
    dictionary: ['/', '&', '5', 'regtest', 's/'],
    maxInputBytes: 16,
    fuzz(input) {
      const t0 = Date.now();
      const val = asPrintable(input, 16);
      if (!val) return;
      const hasSlash = val.includes('/');
      const hasAmp = val.includes('&');
      finishedWithin(t0, 50, 'sedSpecials');
      invariant(
        !hasSlash && !hasAmp,
        `sed injection class: ${hasSlash ? 'delimiter /' : 'replacement &'} in ${JSON.stringify(val)} (s/__BTQ_NODES__/${val}/g)`,
      );
    },
  },
  {
    name: 'k8s.currentNetworkFile',
    project: 'k8s',
    description: 'cat .rendered/network with no whitelist before render_manifests / eval',
    seeds: ['network-file.txt', 'network-ok.txt'],
    dictionary: ['regtest', 'test', 'signet', 'main', 'foo', ';', '\n'],
    maxInputBytes: 48,
    fuzz(input) {
      const t0 = Date.now();
      const cat = input.toString('utf8').replace(/[^\x20-\x7e\n]/g, '');
      if (!cat) return;
      // $(current_network) strips trailing newlines only — no valid_network check.
      const passedToRender = cat.replace(/\n+$/, '');
      if (!passedToRender) return;
      finishedWithin(t0, 50, 'currentNetworkFile');
      invariant(
        ALLOWED_NETWORKS.has(passedToRender),
        `unvalidated .rendered/network ${JSON.stringify(passedToRender)} passed to render_manifests/eval`,
      );
    },
  },
  {
    name: 'k8s.podForNode',
    project: 'k8s',
    description: 'btq-node-${idx} must be a DNS-1123 label; non-numeric idx is not validated',
    seeds: ['pod-idx.txt', 'pod-dotdot.txt'],
    dictionary: [';id', '../', '-1', '0', '3', ';'],
    maxInputBytes: 40,
    fuzz(input) {
      const t0 = Date.now();
      const idx = asPrintable(input, 40);
      if (!idx) return;
      const name = podForNode(idx);
      finishedWithin(t0, 50, 'podForNode');
      invariant(
        isDns1123Label(name),
        `non DNS-1123 pod name ${JSON.stringify(name)} from idx ${JSON.stringify(idx)}`,
      );
    },
  },
  {
    name: 'k8s.contentLength',
    project: 'k8s',
    description: 'int(Content-Length) is unbounded — _read_json will rfile.read(n)',
    seeds: ['content-length.txt', 'content-length-junk.txt'],
    dictionary: ['0', '100', '1000001', '999999999', 'abc'],
    maxInputBytes: 32,
    fuzz(input) {
      const t0 = Date.now();
      const header = asPrintable(input, 32);
      const parsed = pythonIntOrZero(header);
      finishedWithin(t0, 50, 'contentLength');
      invariant(parsed.ok, `uncaught int(Content-Length) on ${JSON.stringify(header)}`);
      invariant(
        parsed.value <= 1_000_000n,
        `unbounded _read_json: Content-Length=${parsed.value} > 1000000`,
      );
    },
  },
  {
    name: 'k8s.healPort',
    project: 'k8s',
    description: 'heal addnode uses 18444 if regtest else 18333 — wrong for signet/main',
    seeds: ['heal-signet.txt', 'heal-main.txt'],
    dictionary: ['regtest', 'test', 'signet', 'main', 'mainnet'],
    maxInputBytes: 24,
    fuzz(input) {
      const t0 = Date.now();
      const network = asPrintable(input, 24).trim();
      if (!network) return;
      const expected = expectedP2P(network);
      if (expected === undefined) return;
      const used = healP2P(network);
      finishedWithin(t0, 50, 'healPort');
      invariant(
        used === expected,
        `heal port bug: network=${network} uses ${used}, should be ${expected}`,
      );
    },
  },
  {
    name: 'k8s.dnsLabel',
    project: 'k8s',
    description: 'node_dns(i)=btq-node-{i}.{headless} — huge/negative i is not DNS-1123',
    seeds: ['dns-idx.txt', 'dns-huge.txt'],
    dictionary: ['-1', '0', '7', '9999999999999999999999999999999999999999999999999999999'],
    maxInputBytes: 80,
    fuzz(input) {
      const t0 = Date.now();
      const raw = asPrintable(input, 80);
      if (!raw || !/^-?\d+$/.test(raw)) return;
      const fqdn = nodeDns(raw);
      const label = fqdn.split('.')[0] ?? '';
      const negative = raw.startsWith('-');
      finishedWithin(t0, 50, 'dnsLabel');
      invariant(
        !negative && isDns1123Label(label),
        `node_dns(${raw}) label ${JSON.stringify(label)} is not DNS-1123 (len=${label.length})`,
      );
    },
  },
  {
    name: 'k8s.grafanaDashboard',
    project: 'k8s',
    description: 'mutated dashboard JSON: parse must not throw; schemaVersion is a number if present',
    seeds: ['dashboard-ok.json', 'dashboard-schema-str.json'],
    dictionary: ['schemaVersion', '39', '"39"', 'null', '{', '}'],
    maxInputBytes: 4_096,
    fuzz(input) {
      const t0 = Date.now();
      let parsed: unknown;
      try {
        parsed = JSON.parse(input.toString('utf8'));
      } catch {
        finishedWithin(t0, 80, 'grafanaDashboard');
        return;
      }
      finishedWithin(t0, 80, 'grafanaDashboard');
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return;
      if (!Object.prototype.hasOwnProperty.call(parsed, 'schemaVersion')) return;
      const v = (parsed as { schemaVersion: unknown }).schemaVersion;
      invariant(typeof v === 'number' && Number.isFinite(v), `schemaVersion must be number, got ${JSON.stringify(v)}`);
    },
  },
  {
    name: 'k8s.xssEvents',
    project: 'k8s',
    description: 'kind/msg/best_hash interpolated into innerHTML — < and javascript: XSS',
    seeds: ['xss-events.json', 'xss-ok.json'],
    dictionary: ['<', 'javascript:', '<img', 'heal', 'best_hash'],
    maxInputBytes: 256,
    fuzz(input) {
      const t0 = Date.now();
      const { kind, msg, best_hash } = splitEventFields(input);
      // Mirror renderEvents / node card interpolation (no escape).
      void `<div class="event" data-kind="${kind}"><span class="event-msg">${msg}</span></div>`;
      void `<div class="node-hash">${best_hash}</div>`;
      finishedWithin(t0, 50, 'xssEvents');
      invariant(
        !xssField(kind) && !xssField(msg) && !xssField(best_hash),
        `XSS innerHTML: kind/msg/best_hash contains < or javascript:`,
      );
    },
  },
  {
    name: 'k8s.scaleBounds',
    project: 'k8s',
    description: 'scale N must be 1..20 — numeric strings outside that range must not be accepted',
    seeds: ['scale-n.txt', 'scale-zero.txt'],
    dictionary: ['0', '1', '20', '21', '100', '-1'],
    maxInputBytes: 24,
    fuzz(input) {
      const t0 = Date.now();
      const n = asPrintable(input, 24).trim();
      if (!/^-?\d+$/.test(n)) return;
      finishedWithin(t0, 50, 'scaleBounds');
      invariant(scaleAccepts(n), `scale node count ${JSON.stringify(n)} outside 1..20`);
    },
  },
  {
    name: 'k8s.yamlQuantity',
    project: 'k8s',
    description: 'sed-written memory/storage must be a k8s quantity if they look like resources',
    seeds: ['quantity.txt', 'quantity-ok.txt'],
    dictionary: ['1Gi', '1Gi;id', '256Mi', '10Gi', ';', '2Gi'],
    maxInputBytes: 32,
    fuzz(input) {
      const t0 = Date.now();
      const qty = asPrintable(input, 32).trim();
      if (!qty) return;
      const written = `memory: "${qty}"\nstorage: ${qty}`;
      finishedWithin(t0, 50, 'yamlQuantity');
      if (!looksLikeResource(qty)) return;
      invariant(
        QUANTITY.test(qty),
        `non-quantity resource still written: ${JSON.stringify(qty)} → ${written.replace(/\n/g, ' | ')}`,
      );
    },
  },
];

export default extra;
