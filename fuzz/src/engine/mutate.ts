const INTERESTING_8 = [0, 1, 0x7f, 0x80, 0xff];
const INTERESTING_32 = [0, 1, -1, 0x7fffffff, 0x80000000, 0xffffffff];

export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function randInt(rng: () => number, n: number): number {
  return Math.floor(rng() * n);
}

function pick<T>(rng: () => number, xs: T[]): T {
  return xs[randInt(rng, xs.length)];
}

/** AFL-style havoc mutations plus dictionary splicing. */
export function mutate(
  input: Buffer,
  rng: () => number,
  dictionary: string[] = [],
  maxBytes = 65_536,
): Buffer {
  let buf = Buffer.from(input);
  const havoc = 1 + randInt(rng, 8);
  for (let i = 0; i < havoc; i++) {
    if (buf.length === 0) {
      buf = Buffer.from([randInt(rng, 256)]);
      continue;
    }
    const op = randInt(rng, 12);
    switch (op) {
      case 0: {
        const i = randInt(rng, buf.length);
        buf[i] ^= 1 << randInt(rng, 8);
        break;
      }
      case 1: {
        const i = randInt(rng, buf.length);
        buf[i] = pick(rng, INTERESTING_8);
        break;
      }
      case 2: {
        const i = randInt(rng, buf.length);
        buf[i] = randInt(rng, 256);
        break;
      }
      case 3: {
        const n = 1 + randInt(rng, 16);
        const extra = Buffer.alloc(n);
        for (let j = 0; j < n; j++) extra[j] = randInt(rng, 256);
        const at = randInt(rng, buf.length + 1);
        buf = Buffer.concat([buf.subarray(0, at), extra, buf.subarray(at)]);
        break;
      }
      case 4: {
        const n = 1 + randInt(rng, Math.min(16, buf.length));
        const at = randInt(rng, buf.length - n + 1);
        buf = Buffer.concat([buf.subarray(0, at), buf.subarray(at + n)]);
        break;
      }
      case 5: {
        if (buf.length < 4) break;
        const at = randInt(rng, buf.length - 3);
        const v = pick(rng, INTERESTING_32) >>> 0;
        buf.writeUInt32LE(v, at);
        break;
      }
      case 6: {
        const times = 2 + randInt(rng, 8);
        if (buf.length * times > maxBytes) break;
        buf = Buffer.concat(Array.from({ length: times }, () => buf));
        break;
      }
      case 7: {
        const token = dictionary.length
          ? Buffer.from(pick(rng, dictionary))
          : Buffer.from([0x00, 0xff, 0x0a, 0x0d]);
        const at = randInt(rng, buf.length + 1);
        buf = Buffer.concat([buf.subarray(0, at), token, buf.subarray(at)]);
        break;
      }
      case 8: {
        const utf = pick(rng, [
          '\u0000',
          '\u202e',
          '\ufffd',
          '🚀',
          '\"',
          '\'',
          '`',
          '${',
          '../',
          '%00',
          '%2e%2e',
          '<script>',
          '__proto__',
          'constructor',
        ]);
        const at = randInt(rng, buf.length + 1);
        const t = Buffer.from(utf);
        buf = Buffer.concat([buf.subarray(0, at), t, buf.subarray(at)]);
        break;
      }
      case 9: {
        buf = Buffer.from(buf.toString('utf8').toUpperCase());
        break;
      }
      case 10: {
        const hex = buf.toString('hex');
        buf = Buffer.from(hex);
        break;
      }
      case 11: {
        const s = buf.toString('utf8');
        try {
          const obj = JSON.parse(s);
          if (obj && typeof obj === 'object') {
            (obj as Record<string, unknown>)[pick(rng, ['__proto__', 'constructor', 'x', ''])] =
              pick(rng, [null, [], {}, '1'.repeat(1000), Number.NaN]);
            buf = Buffer.from(JSON.stringify(obj));
          }
        } catch {
          buf = Buffer.from(`{"q":${JSON.stringify(s.slice(0, 200))}}`);
        }
        break;
      }
    }
    if (buf.length > maxBytes) buf = buf.subarray(0, maxBytes);
  }
  return buf;
}

export function loadSeeds(files: string[], fallback: Buffer[]): Buffer[] {
  return fallback.length ? fallback : [Buffer.from('a')];
}
