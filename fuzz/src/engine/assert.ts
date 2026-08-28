export class InvariantError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'InvariantError';
  }
}

export function invariant(cond: unknown, message: string): asserts cond {
  if (!cond) throw new InvariantError(message);
}

export function isFiniteNumber(n: unknown): n is number {
  return typeof n === 'number' && Number.isFinite(n);
}

export function finishedWithin(start: number, budgetMs: number, label: string): void {
  const elapsed = Date.now() - start;
  invariant(elapsed <= budgetMs, `${label} exceeded ${budgetMs}ms (took ${elapsed}ms)`);
}
