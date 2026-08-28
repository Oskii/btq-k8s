import { loadTargets } from './registry.js';
import { runTarget, summarize } from './engine/run.js';
import type { RunOptions } from './types.js';

function parseArgs(argv: string[]): RunOptions & { list: boolean } {
  const opts: RunOptions & { list: boolean } = {
    iterations: 200,
    timeoutMs: 250,
    saveCrashes: true,
    list: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--list') opts.list = true;
    else if (a === '--iterations' || a === '-n') opts.iterations = Number(argv[++i]);
    else if (a === '--timeout-ms') opts.timeoutMs = Number(argv[++i]);
    else if (a === '--seed') opts.seed = Number(argv[++i]);
    else if (a === '--no-save') opts.saveCrashes = false;
    else if (a === '--target') opts.only = argv[++i];
  }
  return opts;
}

async function main(): Promise<void> {
  const opts = parseArgs(process.argv.slice(2));
  const targets = await loadTargets();
  const selected = opts.only
    ? targets.filter((t) => t.name === opts.only || t.name.includes(opts.only!))
    : targets;

  if (opts.list) {
    for (const t of selected) {
      console.log(`${t.name.padEnd(42)} ${t.description}`);
    }
    console.log(`\n${selected.length} targets`);
    return;
  }

  if (selected.length === 0) {
    console.error('No targets matched.');
    process.exit(2);
  }

  console.log(`Running ${selected.length} targets × ${opts.iterations} iters (timeout ${opts.timeoutMs}ms)\n`);
  const results = [];
  for (const t of selected) {
    process.stdout.write(`→ ${t.name} ... `);
    const r = await runTarget(t, opts, (opts.seed ?? 1) + t.name.length);
    results.push(r);
    console.log(
      r.crashes.length
        ? `${r.crashes.length} finding(s) in ${r.elapsedMs}ms`
        : `ok ${r.iterations} in ${r.elapsedMs}ms`,
    );
  }
  console.log('\n' + summarize(results));
  process.exit(results.reduce((n, r) => n + r.crashes.length, 0) ? 1 : 0);
}

main().catch((err) => {
  console.error(err);
  process.exit(2);
});
