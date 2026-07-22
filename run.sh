#!/usr/bin/env bash
# =============================================================================
# 백엔드(uvicorn service.api:app) 실행 제어 — 이 레포 단독으로 올리고 내린다.
#
#   ./run.sh start      # uvicorn 기동
#   ./run.sh stop
#   ./run.sh restart
#   ./run.sh status
#
# 전제(최초 1회): 3레포 editable 설치(코어+백엔드) + 이 레포의 .env.dev 준비.
#   상세 = README §설치·사용법.  설정 없이 실행 앱 설정은 init_settings(.env.dev)이 로드.
#
# 포트: 기본 8001(프론트 web·admin Vite 프록시 대상).  바꾸려면 BACKEND_PORT=8000 ./run.sh start
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"       # 백엔드 레포 루트

# ── 설정(환경변수로 override 가능) ───────────────────────────────────────────
CONDA_BASE="${CONDA_BASE:-/opt/anaconda3}"
CONDA_ENV="${CONDA_ENV:-AuroraFS}"
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-8001}"                       # 프론트 Vite 프록시 대상 = 127.0.0.1:8001
RUN_DIR="${RUN_DIR:-$HOME/.dataflatform/backend}"          # pid·log
# ────────────────────────────────────────────────────────────────────────────

if [[ -r "$CONDA_BASE/etc/profile.d/conda.sh" ]]; then
  # shellcheck disable=SC1091
  source "$CONDA_BASE/etc/profile.d/conda.sh"; conda activate "$CONDA_ENV"
else
  echo "경고: conda.sh 없음($CONDA_BASE) — CONDA_BASE 확인" >&2
fi
set -uo pipefail

mkdir -p "$RUN_DIR"
NAME=backend

is_running() { local p; [[ -f "$RUN_DIR/$NAME.pid" ]] && p="$(cat "$RUN_DIR/$NAME.pid")" 2>/dev/null && kill -0 "$p" 2>/dev/null; }

do_start() {
  echo "═══ 백엔드 기동 ═══"
  if ! python - <<'PY'
import importlib.util as u, sys
miss=[m for m in ("uvicorn","src","service") if u.find_spec(m) is None]
if miss: print("  누락 모듈:", ", ".join(miss)); print("  → 코어+백엔드 editable 설치 필요"); sys.exit(1)
PY
  then return 1; fi
  if is_running; then printf '  = %-13s 이미 실행 중(pid %s)\n' "$NAME" "$(cat "$RUN_DIR/$NAME.pid")"; return 0; fi
  ( cd "$HERE" && exec uvicorn service.api:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" ) >"$RUN_DIR/$NAME.log" 2>&1 &
  local pid=$!; echo "$pid" >"$RUN_DIR/$NAME.pid"
  printf '  ▶ %-13s pid %-7s → %s\n' "$NAME" "$pid" "$RUN_DIR/$NAME.log"
  sleep 3
  if is_running; then
    printf '  ✔ %-13s (pid %s)\n' "$NAME" "$(cat "$RUN_DIR/$NAME.pid")"
    echo "  · 백엔드 API : http://localhost:$BACKEND_PORT  (프론트 Vite 프록시 대상)"
  else
    printf '  ✗ %-13s 즉시 종료 — 로그:\n' "$NAME"; tail -n 5 "$RUN_DIR/$NAME.log" 2>/dev/null | sed 's/^/      /'
    return 1
  fi
}
do_stop() {
  echo "═══ 백엔드 중지 ═══"
  local pidf="$RUN_DIR/$NAME.pid" pid
  if [[ ! -f "$pidf" ]]; then echo "  - pid 없음(미기동?)"; return; fi
  pid="$(cat "$pidf")"
  if ! kill -0 "$pid" 2>/dev/null; then echo "  - 이미 종료"; rm -f "$pidf"; return; fi
  printf '  ▪ %-13s 종료(pid %s)…' "$NAME" "$pid"
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  if kill -0 "$pid" 2>/dev/null; then printf ' 강제(SIGKILL)'; kill -KILL "$pid" 2>/dev/null || true; fi
  printf ' 완료\n'; rm -f "$pidf"
}
do_status() {
  echo "═══ 백엔드 상태 ═══"
  if is_running; then printf '  ✔ %-13s (pid %s) → http://localhost:%s\n' "$NAME" "$(cat "$RUN_DIR/$NAME.pid")" "$BACKEND_PORT"
  else printf '  ✗ %-13s (중지)\n' "$NAME"; fi
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)  do_status ;;
  *) echo "사용법: $(basename "$0") <start|stop|restart|status>"; exit 2 ;;
esac
