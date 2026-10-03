#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
repository=${1:-fiftycommit/nuage}
case "$repository" in *[!a-zA-Z0-9_./-]*|/*|*/|*/*/*) printf '%s\n' 'Invalid repository'; exit 1;; esac
gh auth status
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then git init -b main; fi
# An unrelated parent repository must never become Nuage's repository.
test "$(git rev-parse --show-toplevel)" = "$(pwd -P)"
git add .
sh scripts/pre-push-check.sh
if ! git diff --cached --quiet; then git commit -m 'feat: publish Nuage portal with CI and Azure deployment'; fi
if gh repo view "$repository" --json visibility >/dev/null 2>&1; then
  visibility=$(gh repo view "$repository" --json visibility --jq '.visibility')
  test "$visibility" = PUBLIC
  if ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin "https://github.com/$repository.git"
  fi
else
  gh repo create "$repository" --public --source=. --remote=origin \
    --description 'Portail de transferts : vérification des liens, reprise, RAR, ZIP et liens sécurisés'
fi
origin=$(git remote get-url origin)
case "$origin" in "https://github.com/$repository.git"|"git@github.com:$repository.git") ;; *) printf '%s\n' 'Unexpected origin; no push performed'; exit 1;; esac
git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push -u origin main
