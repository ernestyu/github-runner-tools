#!/usr/bin/env bash
set -Eeuo pipefail
STATE=/run/grt-fake-systemctl
mkdir -p "$STATE"

pidfile() { printf '%s/%s.pid' "$STATE" "$1"; }
enabledfile() { printf '%s/%s.enabled' "$STATE" "$1"; }
alive() {
  local f; f="$(pidfile "$1")"
  [[ -f "$f" ]] || return 1
  local pid; pid="$(cat "$f")"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}
stop_service() {
  local svc="$1" f pid
  f="$(pidfile "$svc")"
  if [[ -f "$f" ]]; then
    pid="$(cat "$f" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 40); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.05
      done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$f"
  fi
}
start_service() {
  local svc="$1" log="$STATE/$svc.log"
  alive "$svc" && return 0
  case "$svc" in
    github-runner-tools-executor.service)
      mkdir -p /run/github-runner-tools
      chown root:root /run/github-runner-tools
      chmod 0755 /run/github-runner-tools
      nohup /usr/bin/python3 /usr/local/lib/github-runner-tools/web/v2_executor.py         </dev/null >"$log" 2>&1 &
      echo $! >"$(pidfile "$svc")"
      for _ in $(seq 1 100); do
        [[ -S /run/github-runner-tools/executor.sock ]] && return 0
        alive "$svc" || { cat "$log" >&2 || true; return 1; }
        sleep 0.05
      done
      cat "$log" >&2 || true
      return 1
      ;;
    github-runner-tools-web.service)
      bind="$(sed -n 's/^WEB_BIND_ADDRESS=//p' /etc/github-runner-tools/web.conf)"
      nohup /usr/sbin/runuser -u grt-web --         /usr/bin/python3 /usr/local/lib/github-runner-tools/web/app.py         --config /etc/github-runner-tools/web.conf         --auth-config /etc/github-runner-tools/web-auth.conf         </dev/null >"$log" 2>&1 &
      echo $! >"$(pidfile "$svc")"
      for _ in $(seq 1 100); do
        if /usr/bin/curl --noproxy '*' -fsS "http://$bind:8765/login" >/dev/null 2>&1; then
          return 0
        fi
        alive "$svc" || { cat "$log" >&2 || true; return 1; }
        sleep 0.05
      done
      cat "$log" >&2 || true
      return 1
      ;;
    *)
      echo "unsupported fake systemd service: $svc" >&2
      return 1
      ;;
  esac
}

cmd="${1:-}"; shift || true
case "$cmd" in
  daemon-reload) exit 0 ;;
  enable)
    [[ "${1:-}" == "--now" ]] && shift
    svc="${1:-}"; [[ -n "$svc" ]] || exit 2
    : >"$(enabledfile "$svc")"
    start_service "$svc"
    ;;
  start)
    start_service "${1:-}"
    ;;
  restart)
    svc="${1:-}"; stop_service "$svc"; start_service "$svc"
    ;;
  stop)
    stop_service "${1:-}"
    ;;
  disable)
    [[ "${1:-}" == "--now" ]] && { shift; stop_service "${1:-}"; }
    rm -f "$(enabledfile "${1:-}")"
    ;;
  is-active)
    [[ "${1:-}" == "--quiet" ]] && shift
    alive "${1:-}"
    ;;
  is-enabled)
    [[ "${1:-}" == "--quiet" ]] && shift
    [[ -f "$(enabledfile "${1:-}")" ]]
    ;;
  list-unit-files)
    for svc in github-runner-tools-executor.service github-runner-tools-web.service; do
      if [[ -f "$(enabledfile "$svc")" ]]; then
        printf '%s enabled\n' "$svc"
      fi
    done
    ;;
  *)
    echo "unsupported fake systemctl command: $cmd $*" >&2
    exit 2
    ;;
esac
