#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

python3 -m py_compile main.py database.py desire.py desire_store.py
python3 -m pytest -q tests/test_*.py
