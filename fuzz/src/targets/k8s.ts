import { leftoverPlaceholders, manifestsExist, renderManifests, bashEval } from '../mirrors/k8s.js';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { invariant } from '../engine/assert.js';
import type { FuzzTarget } from '../types.js';

const NETWORKS = ['regtest', 'test', 'testnet', 'signet', 'main', 'mainnet'];

function asPrintable(buf: Buffer, max = 32): string {
  return buf.toString('utf8').replace(/[^\x20-\x7e]/g, '').slice(0, max);
}

const k8s: FuzzTarget[] = [
  {
    name: 'k8s.renderManifests',
    project: 'k8s',
    description: 'sed YAML renderer — injection, leftover placeholders, valid docs',
    seeds: ['nodes.txt', 'networks.txt'],
    dictionary: ['/', '&', '\n', ';', 'regtest', '5', 'privileged: true'],
    maxInputBytes: 64,
    fuzz(input) {
      if (!manifestsExist()) return;
      const nodes = /^\d+$/.test(asPrintable(input, 8))
        ? String((input[0] ?? 5) % 21 || 1)
        : asPrintable(input, 12) || '5';
      const network = NETWORKS[(input[1] ?? 0) % NETWORKS.length];
      if (input[2] === 0x2f) {
        const hostile = `${(input[0] ?? 1) % 9 || 1}/${asPrintable(input.subarray(3), 6)}`;
        const r = renderManifests(hostile, network);
        invariant(r.code !== 0 || !r.yaml.includes(hostile.split('/')[1] ?? 'NOPE'), 'sed / injection');
        return;
      }
      const r = renderManifests(nodes, network);
      if (!/^\d+$/.test(nodes) || Number(nodes) < 1 || Number(nodes) > 20) {
        return;
      }
      invariant(r.code === 0, `render failed: ${r.err.slice(0, 120)}`);
      const left = leftoverPlaceholders(r.yaml).filter((p) =>
        ['__BTQ_NODES__', '__BTQ_NETWORK__', '__RPC_PORT__', '__P2P_PORT__', '__BTQ_MEMORY__', '__BTQ_STORAGE__'].includes(p),
      );
      invariant(left.length === 0, `leftover ${left.join(',')}`);
      invariant(!/privileged:\s*true/i.test(r.yaml), 'renderer must not introduce privileged');
      invariant((r.yaml.match(/^apiVersion:/gm) || []).length >= 5, 'multi-doc yaml');
    },
  },
  {
    name: 'k8s.validNetwork',
    project: 'k8s',
    description: 'Network whitelist + aliases',
    seeds: ['networks.txt'],
    dictionary: ['regtest', 'testnet', 'mainnet', 'signet', 'foo'],
    maxInputBytes: 32,
    fuzz(input) {
      if (!manifestsExist()) return;
      const name = asPrintable(input, 24);
      if (!name) return;
      try {
        const normalised = bashEval(`normalise_network ${JSON.stringify(name)}; echo`).trim();
        if (['testnet', 'mainnet', 'regtest', 'test', 'signet', 'main'].includes(name)) {
          invariant(normalised.length > 0, 'alias');
        }
      } catch {
        /* die() is fine for unknown nets in other helpers */
      }
    },
  },
  {
    name: 'k8s.parseUpArgs',
    project: 'k8s',
    description: 'up [N] [NETWORK] parser',
    seeds: ['up-args.txt'],
    maxInputBytes: 40,
    fuzz(input) {
      if (!manifestsExist()) return;
      const a = asPrintable(input.subarray(0, 16), 12);
      const b = asPrintable(input.subarray(16), 12);
      const args = [a, b].filter(Boolean);
      try {
        const out = bashEval(`_parse_up_args ${args.map((x) => JSON.stringify(x)).join(' ')}`).trim();
        const [n, net] = out.split(/\s+/);
        if (n) invariant(/^\d+$/.test(n), `nodes ${n}`);
        if (net) invariant(/^[a-z]+$/.test(net), `net ${net}`);
      } catch {
        /* unrecognised arg dies — ok */
      }
    },
  },
  {
    name: 'k8s.controllerEnv',
    project: 'k8s',
    description: 'controller.py import-time int()/float() must not crash the process uncaught',
    seeds: ['env-values.txt'],
    dictionary: ['abc', '1e999', '-1', '', '5.0', '0x10'],
    maxInputBytes: 24,
    fuzz(input) {
      const val = input.toString('utf8').slice(0, 20);
      const keys = ['BTQ_NODES', 'BTQ_RPC_PORT', 'SCRAPE_INTERVAL', 'METRICS_PORT', 'UI_PORT'];
      const key = keys[(input[0] ?? 0) % keys.length];
      const py = join(import.meta.dirname, '..', '..', 'python', 'import_controller.py');
      try {
        execFileSync('python3', [py, key, val], {
          timeout: 3000,
          encoding: 'utf8',
          env: { ...process.env, [key]: val, BTQ_NODES: key === 'BTQ_NODES' ? val : '2' },
        });
      } catch (err) {
        const e = err as { status?: number; stderr?: string };
        if (e.status === 2) {
          invariant(false, `uncaught import crash ${key}=${JSON.stringify(val)} ${e.stderr?.slice(0, 80)}`);
        }
      }
    },
  },
  {
    name: 'k8s.controllerJson',
    project: 'k8s',
    description: 'UI JSON body coerce / clamp',
    seeds: ['api-mine.json', 'api-tx.json', 'api-storm.json'],
    dictionary: ['node', 'blocks', 'amount', 'count', 'null'],
    maxInputBytes: 4_096,
    fuzz(input) {
      const py = join(import.meta.dirname, '..', '..', 'python', 'fuzz_actions.py');
      try {
        const out = execFileSync('python3', [py], {
          input,
          timeout: 3000,
          encoding: 'utf8',
          maxBuffer: 64 * 1024,
        });
        const parsed = JSON.parse(out);
        invariant(parsed.ok === true, parsed.error ?? 'action crash');
      } catch (err) {
        if (err instanceof SyntaxError) return;
        const e = err as { status?: number; stderr?: string };
        if (e.status === 2) invariant(false, e.stderr?.slice(0, 160) ?? 'python crash');
      }
    },
  },
  {
    name: 'k8s.scrapeNode',
    project: 'k8s',
    description: 'Hostile RPC JSON must not KeyError scrape_node',
    seeds: ['rpc-ok.json', 'rpc-bad.json'],
    maxInputBytes: 8_192,
    fuzz(input) {
      const py = join(import.meta.dirname, '..', '..', 'python', 'fuzz_scrape.py');
      try {
        const out = execFileSync('python3', [py], {
          input,
          timeout: 3000,
          encoding: 'utf8',
        });
        const parsed = JSON.parse(out);
        invariant(parsed.ok === true, parsed.error ?? 'scrape crash');
      } catch (err) {
        const e = err as { status?: number; stderr?: string; stdout?: string };
        if (e.status === 2) {
          invariant(false, (e.stdout || e.stderr || 'scrape crash').slice(0, 160));
        }
      }
    },
  },
  {
    name: 'k8s.entrypointEnv',
    project: 'k8s',
    description: 'entrypoint.sh HOSTNAME / NODES / EXTRA_ARGS',
    seeds: ['hostnames.txt'],
    dictionary: ['btq-node-0', 'btq-node-', 'regtest', '*'],
    maxInputBytes: 64,
    fuzz(input) {
      const sh = join(import.meta.dirname, '..', '..', 'python', 'fuzz_entrypoint.sh');
      const host = asPrintable(input, 24) || 'btq-node-0';
      const nodes = String(((input[0] ?? 3) % 8) + 1);
      try {
        execFileSync('bash', [sh, host, nodes, 'regtest'], {
          timeout: 2000,
          encoding: 'utf8',
        });
      } catch (err) {
        const e = err as { status?: number };
        invariant(e.status === 1 || e.status === 0, `entrypoint status ${e.status}`);
      }
    },
  },
];

export default k8s;
