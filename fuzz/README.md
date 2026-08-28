# btq-k8s fuzzing

In-process mutation fuzzer for `bin/btqnet`, `entrypoint.sh`, and `controller.py`.

```bash
# from repo root
./bin/fuzz list
./bin/fuzz smoke
./bin/fuzz                 # 200 iters per target
./bin/fuzz -- --target k8s.scrapeNode -n 2000
```

Or from this directory:

```bash
npm install
npm run smoke
npm run fuzz
```

Findings land in `crashes/`. Exit code 1 means the campaign recorded findings (often known bug classes).
