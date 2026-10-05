#!/bin/bash
# Backup harian agentarium ke GitHub (dasrams31/agentarium)
# Dijalankan via systemd timer tiap hari. Token dibaca dari config/github_token (chmod 600).
# Repo agentarium langsung di-push dari working directory (tidak pakai staging).
set -e
cd "$HOME/workspace/agentarium"
TOKEN_FILE="$HOME/workspace/mc-portal/config/github_token"

git add -A
if git diff --cached --quiet; then
  echo "no changes to push"
  exit 0
fi
git -c user.name="dasrams31" -c user.email="ramadanadipa176@gmail.com" \
  commit -qm "Daily backup $(date +%F)"

TOKEN=<redacted>"$TOKEN_FILE")
git push "https://dasrams31:${TOKEN}@github.com/dasrams31/agentarium.git" main 2>&1 | tail -2
unset TOKEN
echo "pushed $(date -Is)"
