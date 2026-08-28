import { readdirSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import type { FuzzTarget } from './types.js';

const TARGET_DIR = join(import.meta.dirname, 'targets');

export async function loadTargets(): Promise<FuzzTarget[]> {
  const files = readdirSync(TARGET_DIR).filter((f) => f.endsWith('.ts') || f.endsWith('.js'));
  const all: FuzzTarget[] = [];
  for (const file of files) {
    const mod = (await import(pathToFileURL(join(TARGET_DIR, file)).href)) as {
      default?: FuzzTarget[] | FuzzTarget;
      targets?: FuzzTarget[];
    };
    const exported = mod.targets ?? mod.default;
    if (!exported) continue;
    const list = Array.isArray(exported) ? exported : [exported];
    all.push(...list);
  }
  const seen = new Set<string>();
  return all.filter((t) => {
    if (seen.has(t.name)) return false;
    seen.add(t.name);
    return true;
  });
}
