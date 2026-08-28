import { mkdirSync, writeFileSync, readFileSync, existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { mutate, mulberry32 } from './mutate.js';
import { InvariantError } from './assert.js';
import type { CrashRecord, FuzzTarget, RunOptions, TargetResult } from '../types.js';

const ROOT = join(import.meta.dirname, '..', '..');

export function corpusDir(): string {
  return join(ROOT, 'corpus');
}

export function crashDir(target: string): string {
  return join(ROOT, 'crashes', target.replace(/[^\w.-]+/g, '_'));
}

export function readCorpus(seedNames?: string[]): Buffer[] {
  const dir = corpusDir();
  const out: Buffer[] = [];
  if (existsSync(dir)) {
    const files = readdirSync(dir).filter((f) => !f.startsWith('.'));
    const wanted = seedNames?.length ? files.filter((f) => seedNames.includes(f)) : files;
    for (const f of wanted) out.push(readFileSync(join(dir, f)));
  }
  return out.length ? out : [Buffer.from('seed')];
}

export async function runTarget(
  target: FuzzTarget,
  opts: RunOptions,
  rngSeed: number,
): Promise<TargetResult> {
  const rng = mulberry32(rngSeed);
  const seeds = readCorpus(target.seeds);
  const crashes: CrashRecord[] = [];
  const startAll = Date.now();
  const maxBytes = target.maxInputBytes ?? 65_536;
  let hangs = 0;

  for (let i = 0; i < opts.iterations; i++) {
    const base = seeds[Math.floor(rng() * seeds.length)];
    const input = i < seeds.length ? seeds[i] : mutate(base, rng, target.dictionary ?? [], maxBytes);
    const clipped = input.length > maxBytes ? input.subarray(0, maxBytes) : input;
    const t0 = Date.now();
    try {
      const result = target.fuzz(clipped);
      if (result && typeof (result as Promise<void>).then === 'function') {
        await Promise.race([
          result,
          new Promise<never>((_, reject) =>
            setTimeout(() => reject(new Error(`hang: exceeded ${opts.timeoutMs}ms`)), opts.timeoutMs),
          ),
        ]);
      } else if (Date.now() - t0 > opts.timeoutMs) {
        throw new Error(`hang: exceeded ${opts.timeoutMs}ms`);
      }
    } catch (err) {
      const elapsed = Date.now() - t0;
      const message = err instanceof Error ? err.message : String(err);
      const hang = /hang/i.test(message) || elapsed >= opts.timeoutMs;
      if (hang) hangs++;
      const rec: CrashRecord = {
        target: target.name,
        kind: hang ? 'hang' : err instanceof InvariantError ? 'invariant' : 'throw',
        message,
        seedHex: clipped.toString('hex').slice(0, 8192),
        iteration: i,
        elapsedMs: elapsed,
      };
      crashes.push(rec);
      if (opts.saveCrashes) persistCrash(rec, clipped);
    }
  }

  return {
    target: target.name,
    iterations: opts.iterations,
    crashes,
    hangs,
    elapsedMs: Date.now() - startAll,
  };
}

function persistCrash(rec: CrashRecord, input: Buffer): void {
  const dir = crashDir(rec.target);
  mkdirSync(dir, { recursive: true });
  const id = `${rec.kind}-${rec.iteration}-${Date.now()}`;
  writeFileSync(join(dir, `${id}.bin`), input);
  writeFileSync(
    join(dir, `${id}.json`),
    JSON.stringify({ ...rec, savedAt: new Date().toISOString() }, null, 2),
  );
}

export function summarize(results: TargetResult[]): string {
  const lines: string[] = [];
  let totalCrashes = 0;
  let totalHangs = 0;
  for (const r of results) {
    totalCrashes += r.crashes.length;
    totalHangs += r.hangs;
    const flag = r.crashes.length ? 'CRASH' : 'ok';
    lines.push(
      `  [${flag}] ${r.target.padEnd(42)} iters=${String(r.iterations).padStart(4)} crashes=${r.crashes.length} hangs=${r.hangs} ${r.elapsedMs}ms`,
    );
    for (const c of r.crashes.slice(0, 3)) {
      lines.push(`         ${c.kind}: ${c.message.slice(0, 160)}`);
    }
  }
  lines.unshift(
    `Fuzz summary: ${results.length} targets, ${totalCrashes} findings, ${totalHangs} hangs`,
  );
  return lines.join('\n');
}
