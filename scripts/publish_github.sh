#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_NAME="${1:-Transcriptom}"
VISIBILITY="${2:-private}"

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

case "$VISIBILITY" in
  private|public|internal) ;;
  *) fail "Visibility must be private, public, or internal." ;;
esac

command -v gh >/dev/null 2>&1 || fail "GitHub CLI is required. Install it with: brew install gh"
gh auth status >/dev/null 2>&1 || fail "GitHub CLI is not logged in. Run: gh auth login"

cd "$ROOT_DIR"

if [[ ! -d .git ]]; then
  git init
fi

git branch -M main
git add .gitignore README.md install.sh requirements.txt transcriptom transcriptom_app scripts

if ! git diff --cached --quiet; then
  git commit -m "Initial Transcriptom CLI"
fi

if git remote get-url origin >/dev/null 2>&1; then
  git push -u origin main
else
  gh repo create "$REPO_NAME" "--$VISIBILITY" --source=. --remote=origin --push
fi
