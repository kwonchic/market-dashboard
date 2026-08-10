#!/bin/bash
set -euo pipefail

REPO="/Users/killian/.hermes/workspace/market-dashboard-fix"
ENV_FILE="/Users/killian/.toss-cli/market.env"
LOG_DIR="/Users/killian/.hermes/workspace/market-dashboard-fix/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/update_$(date +%Y-%m-%d).log"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] start"
  cd "$REPO"

  if [ ! -f "$ENV_FILE" ]; then
    echo "missing env file: $ENV_FILE"
    exit 2
  fi

  git fetch origin main
  git pull --ff-only origin main

  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a

  /opt/homebrew/bin/python3 scripts/fetch_toss_market.py
  /opt/homebrew/bin/python3 generate.py
  cp dashboard.html index.html
  python3 -m json.tool data.json >/dev/null

  /opt/homebrew/bin/python3 - <<'PY'
from pathlib import Path
import re, sys
bad=[]
for p in Path('.').rglob('*'):
    if '.git' in p.parts or p.is_dir():
        continue
    if p.parts and p.parts[0] == 'logs':
        continue
    try:
        s=p.read_text(errors='ignore')
    except Exception:
        continue
    if re.search(r'tsck_live_[A-Za-z0-9]+|tssk_live_[A-Za-z0-9]+|Bearer\s+[A-Za-z0-9._-]+', s):
        bad.append(str(p))
if bad:
    print('secret_hits', bad)
    sys.exit(3)
print('secret_hits 0')
PY

  if git diff --quiet -- data.json dashboard.html index.html; then
    echo "no changes"
  else
    git add data.json dashboard.html index.html
    git commit -m "Update Toss market dashboard data"
    git push origin main
    echo "pushed"
  fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] done"
} >> "$LOG" 2>&1
