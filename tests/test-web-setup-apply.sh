#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
fail() { echo "FAIL: $*" >&2; exit 1; }

TMP="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT

MOCK="$TMP/mockbin"
LOG="$TMP/sudo.log"
OUT="$TMP/apply.out"
mkdir -p "$MOCK"

cat > "$MOCK/id" <<'MOCK'
#!/usr/bin/env bash
if [[ "$#" -eq 2 && "$1" == "-u" && "$2" == "grt-web" ]]; then echo 29991; exit 0; fi
if [[ "$#" -eq 2 && "$1" == "-g" && "$2" == "grt-web" ]]; then echo 29991; exit 0; fi
exec /usr/bin/id "$@"
MOCK

cat > "$MOCK/getent" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "passwd" && "${2:-}" == "grt-web" ]]; then exit 2; fi
exec /usr/bin/getent "$@"
MOCK

cat > "$MOCK/ps" <<'MOCK'
#!/usr/bin/env bash
if [[ "$*" == "-p 1 -o comm=" ]]; then echo systemd; exit 0; fi
exec /usr/bin/ps "$@"
MOCK

cat > "$MOCK/systemctl" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "is-active" ]]; then exit 0; fi
exit 0
MOCK

cat > "$MOCK/tailscale" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK

cat > "$MOCK/useradd" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK

cat > "$MOCK/systemd-tmpfiles" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK

cat > "$MOCK/sudo" <<MOCK
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$LOG"
if [[ "\${1:-}" == "-v" ]]; then exit 0; fi
case "\${1:-}" in
  test)
    # Fresh-host simulation: no managed Web path or conflicting symlink exists.
    exit 1
    ;;
  stat)
    fmt="\${3:-}"
    case "\$fmt" in
      %U:%G:%a:%F) printf '%s\n' 'root:grt-web:640:regular file' ;;
      %U:%a) printf '%s\n' 'root:755' ;;
      %a) printf '%s\n' '755' ;;
      *) printf '%s\n' 'root:root:755:directory' ;;
    esac
    exit 0
    ;;
  grep)
    exit 1
    ;;
  *)
    exit 0
    ;;
esac
MOCK

chmod +x "$MOCK"/*

# setup-web-management.sh reads the administrator password from /dev/tty.
# util-linux script(1) provides a disposable PTY while every privileged host
# mutation is intercepted by the sudo mock above.
printf 'test-web-password\ntest-web-password\n' |
  script -q -c "PATH='$MOCK':\$PATH bash '$ROOT/scripts/setup-web-management.sh' --apply" /dev/null
  >"$OUT" 2>&1 || true

NORMALIZED_OUT="$(tr -d '\r' < "$OUT")"
if ! grep -Fq "Web Management installed and enabled." <<<"$NORMALIZED_OUT"; then
  cat "$OUT" >&2
  fail "mocked explicit Web --apply path did not reach successful completion"
fi

grep -Fq "useradd --system --user-group --no-create-home --shell /usr/sbin/nologin grt-web" "$LOG" ||
  fail "--apply did not request locked grt-web account"
grep -Fq "install -o root -g grt-web -m 0640" "$LOG" ||
  fail "--apply did not request root:grt-web mode-0640 configuration"
grep -Fq "install -o root -g root -m 0755" "$LOG" ||
  fail "--apply did not request root-owned executable installation"
grep -Fq "systemctl enable github-runner-tools-dispatch.service" "$LOG" ||
  fail "--apply did not enable dispatcher"
grep -Fq "systemctl enable github-runner-tools-web.service" "$LOG" ||
  fail "--apply did not enable Web frontend"
grep -Fq "systemctl restart github-runner-tools-dispatch.service" "$LOG" ||
  fail "--apply did not start dispatcher"
grep -Fq "systemctl restart github-runner-tools-web.service" "$LOG" ||
  fail "--apply did not start Web frontend"

if grep -Eq 'tailscale[[:space:]]+(serve|funnel)' "$LOG"; then
  fail "--apply unexpectedly configured a Tailscale endpoint"
fi
if grep -Eq 'NOPASSWD|sudoers' "$LOG"; then
  fail "--apply attempted to create a passwordless sudo path"
fi

echo "PASS: explicit Web apply installation contract tests"
