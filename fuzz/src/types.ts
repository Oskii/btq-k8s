export interface FuzzTarget {
  name: string;
  project?: string;
  description: string;
  seeds?: string[];
  dictionary?: string[];
  maxInputBytes?: number;
  fuzz(input: Buffer): void | Promise<void>;
}

export interface CrashRecord {
  target: string;
  kind: 'throw' | 'hang' | 'invariant';
  message: string;
  seedHex: string;
  iteration: number;
  elapsedMs: number;
}

export interface RunOptions {
  iterations: number;
  timeoutMs: number;
  seed?: number;
  saveCrashes: boolean;
  only?: string;
}

export interface TargetResult {
  target: string;
  iterations: number;
  crashes: CrashRecord[];
  hangs: number;
  elapsedMs: number;
}
