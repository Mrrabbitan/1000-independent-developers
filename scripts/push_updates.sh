#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -z "$(git status --porcelain)" ]]; then
  echo "没有需要提交的变更"
  exit 0
fi

if [[ "${DRY_RUN:-}" == "1" ]]; then
  echo "DRY_RUN=1，跳过提交与推送"
  git status --porcelain
  exit 0
fi

git add README.md
commit_date="$(date +%Y-%m-%d)"
git commit -m "chore: update README (${commit_date})"
git push
