#!/usr/bin/env bash
# Setup development tools for Gittensor PR creation
# Run: bash scripts/setup_dev.sh
set -euo pipefail

echo "=== GITTENSOR DEV TOOLS SETUP ==="

echo ""
echo "1. Installing Python dev tools..."
pip3 install gitlint yamllint --break-system-packages 2>/dev/null || pip3 install gitlint yamllint

echo ""
echo "2. Installing pre-commit hooks..."
pre-commit install --hook-type pre-commit --hook-type pre-push 2>/dev/null || pre-commit install

echo ""
echo "3. Setting up .env from template..."
if [ ! -f .env ]; then
    cp .env.example .env 2>/dev/null || echo "  (no .env.example found, skipping)"
    echo "  Created .env — edit with your GITHUB_PAT"
else
    echo "  .env already exists"
fi

echo ""
echo "4. Verifying tools..."
for tool in ruff pyright gitlint yamllint gh pre-commit; do
    if command -v "$tool" &>/dev/null; then
        echo "  ✅ $tool"
    else
        echo "  ❌ $tool — MISSING"
    fi
done

echo ""
echo "=== DONE ==="
echo "Quick usage:"
echo "  python3 scripts/pr_dashboard.py       # Check PR status"
echo "  python3 scripts/issue_scout.py --suggest  # Find best issue"
echo "  python3 scripts/pr_creator.py --quick     # Create PR"
echo "  python3 scripts/pre_submit.py             # Validate before push"
