import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { existsSync, mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';

export const K8S_ROOT = join(import.meta.dirname, '..', '..', '..');
export const BTQNET = join(K8S_ROOT, 'bin', 'btqnet');

function libScript(): string {
  const raw = readFileSync(BTQNET, 'utf8');
  return raw
    .replace(/\nmain "\$@"\s*$/, '\n')
    .replace(
      /ROOT="\$\(cd "\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\)\/\.\." && pwd\)"/,
      `ROOT=${JSON.stringify(K8S_ROOT)}`,
    )
    .replace('set -euo pipefail', 'set +euo pipefail');
}

export function bashEval(fnCall: string): string {
  const dir = mkdtempSync(join(tmpdir(), 'btq-fuzz-'));
  const lib = join(dir, 'btqnet-lib.sh');
  writeFileSync(lib, libScript());
  const script = `
set +e
source ${JSON.stringify(lib)}
${fnCall}
`;
  try {
    return execFileSync('bash', ['-c', script], {
      encoding: 'utf8',
      cwd: K8S_ROOT,
      timeout: 4000,
      maxBuffer: 2 * 1024 * 1024,
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

export function renderManifests(nodes: string, network: string): { yaml: string; code: number; err: string } {
  try {
    const yaml = bashEval(`render_manifests ${JSON.stringify(nodes)} ${JSON.stringify(network)}; cat .rendered/manifests.yaml`);
    return { yaml, code: 0, err: '' };
  } catch (err) {
    const e = err as { stdout?: string; stderr?: string; status?: number };
    return { yaml: e.stdout ?? '', code: e.status ?? 1, err: e.stderr ?? String(err) };
  }
}

export function leftoverPlaceholders(yaml: string): string[] {
  return [...yaml.matchAll(/__[A-Z0-9_]+__/g)].map((m) => m[0]);
}

export function manifestsExist(): boolean {
  return existsSync(join(K8S_ROOT, 'k8s', '00-namespace.yaml'));
}

export const QUANTITY = /^\d+(\.\d+)?(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?$/;
export const DNS1123 = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
