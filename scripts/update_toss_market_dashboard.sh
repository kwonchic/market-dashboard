#!/bin/bash
# 국내 시장 데이터 수집(토스증권 Open API) → 공개 대시보드 배포.
#
# 구조 (2026-11-17 수리):
#   1) 수집: 자격증명은 수집 하위 셸에만 주입. 결과는 저장소 밖 아티팩트(JSON)로 먼저 보존.
#      git 상태와 무관하게 항상 실행된다 (git 동기화 실패가 수집을 막지 않음).
#   2) 배포: origin/main 최신 위에 임시 worktree를 만들고 아티팩트의 kr.* 만 반영 → generate → commit → 일반 push.
#      운영 작업본(main 작업트리)은 건드리지 않는다. push 거절 시 fetch부터 재시도(최대 3회). force push 없음.
#   3) 운영 작업본 코드 갱신: fast-forward 가능할 때만. 갈라져 있으면 경고만 남기고 배포는 계속.
#
# 종료코드: 0 성공 · 10 수집 일부 실패(배포는 수행) · 11 수집 전부 실패(실패 표시만 배포)
#           20 git fetch 실패 · 21 push 실패(재시도 소진) · 22 빌드/검증 실패 · 30 다른 실행 진행 중 · 2 env 파일 없음
# 상태: $STATE_DIR/status.json (마지막 성공 배포 시각·커밋은 실패해도 보존), 아티팩트: $STATE_DIR/artifacts/
set -uo pipefail

REPO="${MDASH_REPO:-/Users/killian/.hermes/workspace/market-dashboard-fix}"
ENV_FILE="${MDASH_ENV_FILE:-/Users/killian/.toss-cli/market.env}"
LOG_DIR="${MDASH_LOG_DIR:-$REPO/logs}"
STATE_DIR="${MDASH_STATE_DIR:-$LOG_DIR/state}"
PY="${MDASH_PYTHON:-/opt/homebrew/bin/python3}"
REMOTE="${MDASH_REMOTE:-origin}"
BRANCH="main"
PUSH_RETRIES="${MDASH_PUSH_RETRIES:-3}"
LOCK_STALE_SEC="${MDASH_LOCK_STALE_SEC:-7200}"

mkdir -p "$LOG_DIR" "$STATE_DIR/artifacts"
LOG="$LOG_DIR/update_$(date +%Y-%m-%d).log"
exec >>"$LOG" 2>&1

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

# ── 상태 기록 (마지막 성공 값은 보존) ───────────────────────────────────
write_status() {  # $1=phase $2=result $3=exit_code [$4=commit]
  "$PY" - "$STATE_DIR/status.json" "$1" "$2" "$3" "${4:-}" "${ART:-}" "${COLLECT_RC:-}" <<'PY'
import json, os, sys, datetime
path, phase, result, code, commit, art, crc = sys.argv[1:8]
now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat(timespec="seconds")
try:
    st = json.load(open(path, encoding="utf-8"))
except Exception:
    st = {}
st.update({"last_run_at": now, "last_phase": phase, "last_result": result, "last_exit": int(code),
           "last_artifact": art or st.get("last_artifact"), "last_collect_rc": int(crc) if crc else None})
if phase == "deploy" and result in ("pushed", "noop"):
    st["last_deploy_success_at"] = now
    if commit:
        st["last_deploy_commit"] = commit
if phase == "collect" and result in ("ok", "partial"):
    st["last_collect_success_at"] = now
tmp = path + ".tmp"
json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
}

# ── 동시 실행 방지 (원자적 mkdir 잠금, 죽은 잠금은 회수) ─────────────────
LOCK="$STATE_DIR/run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
  age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || stat -c %Y "$LOCK" 2>/dev/null || date +%s) ))
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null || [ "$age" -gt "$LOCK_STALE_SEC" ]; then
    log "stale lock (pid=${holder:-?} age=${age}s) → 회수"
    rm -rf "$LOCK"; mkdir "$LOCK" || { log "lock race lost"; exit 30; }
  else
    log "다른 실행 진행 중(pid=${holder:-?}) → 이번 회차 건너뜀"
    exit 30
  fi
fi
echo $$ > "$LOCK/pid"
WT=""
cleanup() {
  if [ -n "$WT" ] && [ -d "$WT" ]; then git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"; fi
  git -C "$REPO" worktree prune >/dev/null 2>&1 || true
  rm -rf "$LOCK"
}
trap cleanup EXIT

log "start"
cd "$REPO" || exit 22

# ── 0) 원격 동기화 (실패해도 수집은 진행) ─────────────────────────────────
FETCH_OK=1
if ! git fetch "$REMOTE" "$BRANCH"; then
  FETCH_OK=0; log "git fetch 실패 → 수집만 진행, 배포 보류"
fi
if [ "$FETCH_OK" = 1 ]; then
  if git merge-base --is-ancestor HEAD "$REMOTE/$BRANCH" && git diff --quiet && git diff --cached --quiet; then
    git merge --ff-only -q "$REMOTE/$BRANCH" && log "운영 작업본 코드 fast-forward → $(git rev-parse --short HEAD)"
  else
    log "경고: 운영 작업본이 $REMOTE/$BRANCH 와 갈라짐/변경 있음 (ahead/behind=$(git rev-list --left-right --count HEAD...$REMOTE/$BRANCH | tr '\t' '/')). 코드 갱신 생략, 배포는 임시 worktree로 계속"
  fi
fi

# ── 1) 수집 → 아티팩트 ───────────────────────────────────────────────────
ART="$STATE_DIR/artifacts/kr_collect_$(date +%Y%m%dT%H%M%S)_$$.json"
if [ -n "${MDASH_COLLECT_CMD:-}" ]; then
  # 테스트용 대체 수집기: 아티팩트 경로를 인자로 받는다 (자격증명 불필요)
  bash -c "$MDASH_COLLECT_CMD \"\$1\"" _ "$ART"; COLLECT_RC=$?
else
  if [ ! -f "$ENV_FILE" ]; then log "missing env file"; write_status collect missing_env 2; exit 2; fi
  # 자격증명은 이 하위 셸에만 존재 (git·generate 단계로 새지 않음)
  ( set -a; . "$ENV_FILE"; set +a; exec "$PY" scripts/fetch_toss_market.py --collect-out "$ART" ); COLLECT_RC=$?
fi
if [ ! -s "$ART" ]; then
  log "수집 아티팩트 없음 (rc=$COLLECT_RC)"; write_status collect no_artifact 11; exit 11
fi
cp "$ART" "$STATE_DIR/latest_collect.json"
case "$COLLECT_RC" in
  0) write_status collect ok 0 ;;
  10) write_status collect partial 10 ;;
  *) COLLECT_RC=11; write_status collect failed 11 ;;
esac
log "collect rc=$COLLECT_RC artifact=$ART"

if [ "$FETCH_OK" != 1 ]; then write_status deploy fetch_failed 20; exit 20; fi

# ── 2) 배포: origin 최신 위 임시 worktree에서만 커밋·일반 push ─────────────
attempt=1
while [ "$attempt" -le "$PUSH_RETRIES" ]; do
  if [ "$attempt" -gt 1 ]; then
    git fetch "$REMOTE" "$BRANCH" || { write_status deploy fetch_failed 20; exit 20; }
  fi
  WT="$(mktemp -d "${TMPDIR:-/tmp}/mdash-deploy.XXXXXX")"; rmdir "$WT"
  git worktree add --detach -q "$WT" "$REMOTE/$BRANCH" || { write_status deploy worktree_failed 22; exit 22; }
  if ! ( cd "$WT" \
        && "$PY" scripts/fetch_toss_market.py --apply "$ART" --data data.json \
        && "$PY" generate.py \
        && cp dashboard.html index.html \
        && "$PY" -m json.tool data.json >/dev/null \
        && ! grep -lE 'tsck_live_[A-Za-z0-9]+|tssk_live_[A-Za-z0-9]+|Bearer[[:space:]]+[A-Za-z0-9._-]{8,}' data.json dashboard.html index.html ); then
    log "빌드/검증 실패 (아티팩트 보존: $ART)"; write_status deploy build_failed 22; exit 22
  fi
  if git -C "$WT" diff --quiet -- data.json dashboard.html index.html; then
    log "no changes"; write_status deploy noop "$COLLECT_RC"; exit "$COLLECT_RC"
  fi
  git -C "$WT" add data.json dashboard.html index.html
  git -C "$WT" commit -q -m "Update Toss market dashboard data" || { write_status deploy commit_failed 22; exit 22; }
  NEW="$(git -C "$WT" rev-parse HEAD)"
  if git -C "$WT" push "$REMOTE" "HEAD:refs/heads/$BRANCH"; then
    log "pushed $NEW"; write_status deploy pushed "$COLLECT_RC" "$NEW"
    log "done"; exit "$COLLECT_RC"
  fi
  log "push 실패 (attempt $attempt/$PUSH_RETRIES) → 원격 최신으로 재구성 후 재시도"
  git worktree remove --force "$WT" >/dev/null 2>&1 || rm -rf "$WT"; WT=""
  attempt=$((attempt + 1)); sleep "${MDASH_RETRY_SLEEP:-5}"
done
log "push 재시도 소진. 수집 결과는 보존됨: $ART"
write_status deploy push_failed 21
exit 21
