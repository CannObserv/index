#!/usr/bin/env bash
# The shipping-work skill's gate (#3). Runs exactly what CI runs, in CI's
# order, and stops at the first failure.
#
# CI (.github/workflows/ci.yml) stays the only correctness gate. This file is
# its local mirror, and tests/deploy/test_pre_ship.py fails if the two diverge.
# Add a step to CI and it goes here too.
set -euo pipefail

cd "$(dirname "$0")/.."

uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
