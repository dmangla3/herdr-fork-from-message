#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 - <<'PY'
from pathlib import Path

for path in (*Path("scripts").glob("*.py"), *Path("tests").glob("*.py")):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY

bun test tests/runtime_bridge.test.ts
biome check scripts/runtime_bridge.ts tests/runtime_bridge.test.ts

if command -v herdr >/dev/null 2>&1; then
    herdr --version
fi
if command -v codex >/dev/null 2>&1; then
    codex --version
fi
if command -v claude >/dev/null 2>&1; then
    claude --version
fi
