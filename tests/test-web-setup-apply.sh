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
UNIT_CAPTURE="$TMP/dispatcher.unit"
SCENARIO="$TMP/scenario"
STATE_RECORD="$TMP/lifecycle.record"
printf '%s\n' fresh > "$SCENARIO"
printf 'persistent lifecycle state; must survive reinstall\n' > "$STATE_RECORD"
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
    if [[ "\${3:-}" == "/var/lib/github-runner-tools" ]]; then
      state="\$(cat "$SCENARIO")"
      if [[ "\${2:-}" == "-L" ]]; then
        [[ "\$state" == symlink ]]; exit \$?
      fi
      if [[ "\${2:-}" == "-e" ]]; then
        [[ "\$state" != fresh ]]; exit \$?
      fi
    fi
    # All other managed Web paths are absent.
    exit 1
    ;;
  stat)
    fmt="\${3:-}"
    if [[ "\${4:-}" == "/var/lib/github-runner-tools" ]]; then
      case "\$(cat "$SCENARIO")" in
        fresh|valid) printf '%s\\n' 'root:root:700:directory' ;;
        mode0755) printf '%s\\n' 'root:root:755:directory' ;;
        otherowner) printf '%s\\n' 'grt-web:root:700:directory' ;;
        regularfile) printf '%s\\n' 'root:root:700:regular file' ;;
        symlink) printf '%s\\n' 'root:root:700:symbolic link' ;;
        *) exit 97 ;;
      esac
      exit 0
    fi
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
  install)
    if [[ "\${@: -1}" == "/etc/systemd/system/github-runner-tools-dispatch.service" ]]; then
      cp "\${@: -2:1}" "$UNIT_CAPTURE"
    fi
    exit 0
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

grep -Fq 'install -d -o root -g root -m 0700 /var/lib/github-runner-tools' "$LOG" ||
  fail "--apply did not provision root-only lifecycle state directory"
[[ -f "$UNIT_CAPTURE" ]] || fail "Dispatcher Unit was not captured"
grep -Fxq 'ProtectSystem=strict' "$UNIT_CAPTURE" ||
  fail "Dispatcher strict filesystem protection was disabled"
grep -Eq '^ReadWritePaths=.+ /var/lib/github-runner-tools$' "$UNIT_CAPTURE" ||
  fail "Dispatcher Unit missing lifecycle state write path"

if grep -Eq 'tailscale[[:space:]]+(serve|funnel)' "$LOG"; then
  fail "--apply unexpectedly configured a Tailscale endpoint"
fi
if grep -Eq 'NOPASSWD|sudoers' "$LOG"; then
  fail "--apply attempted to create a passwordless sudo path"
fi


# Exercise the existing-state branch, not only the fresh-install branch.
# script -e propagates the installer's exit status through the disposable PTY.
run_existing_case() {
  local scenario="$1" expected="$2" before after
  printf '%s\n' "$scenario" > "$SCENARIO"
  : > "$LOG"
  before="$(cksum < "$STATE_RECORD")"
  if printf 'test-web-password\ntest-web-password\n' |
    script -e -q -c "PATH='$MOCK':\$PATH bash '$ROOT/scripts/setup-web-management.sh' --apply" /dev/null >"$OUT" 2>&1; then
    [[ "$expected" == success ]] || fail "$scenario: unsafe state accepted"
  else
    [[ "$expected" == reject ]] || { cat "$OUT" >&2; fail "$scenario: valid existing state rejected"; }
  fi
  after="$(cksum < "$STATE_RECORD")"
  [[ "$before" == "$after" ]] || fail "$scenario: persisted lifecycle record changed"
  grep -Fq 'test -L /var/lib/github-runner-tools' "$LOG" ||
    fail "$scenario: symlink test not performed"
  if [[ "$scenario" != symlink ]]; then
    grep -Fq 'test -e /var/lib/github-runner-tools' "$LOG" ||
      fail "$scenario: existing path check not performed"
  fi
  if [[ "$expected" == success ]]; then
    grep -Fq "stat -c %U:%G:%a:%F /var/lib/github-runner-tools" "$LOG" ||
      fail "$scenario: existing directory metadata not checked"
    grep -Fq 'systemctl restart github-runner-tools-dispatch.service' "$LOG" ||
      fail "$scenario: valid existing directory prevented installation"
    if grep -Fq 'install -d -o root -g root -m 0700 /var/lib/github-runner-tools' "$LOG"; then
      fail "$scenario: existing lifecycle state directory recreated"
    fi
  else
    grep -Eq 'ERROR: (Lifecycle state directory must not be a symlink|Existing lifecycle state directory has unexpected ownership/mode/type)' "$OUT" ||
      fail "$scenario: expected fail-closed diagnostic missing"
    if grep -Eq '(^| )(chmod|chown|rm|mv)( |$)|^install -d .* /var/lib/github-runner-tools$|^systemctl (enable|restart|daemon-reload)' "$LOG"; then
      fail "$scenario: unsafe state triggered mutation or service activation"
    fi
    if grep -Eq '^install .* /usr/local/lib/github-runner-tools/web|^install .* /etc/systemd/system/' "$LOG"; then
      fail "$scenario: unsafe state allowed code/unit installation"
    fi
  fi
}

run_existing_case valid success
run_existing_case mode0755 reject
run_existing_case otherowner reject
run_existing_case regularfile reject
run_existing_case symlink reject

echo "PASS: existing lifecycle-state validation, fail-closed behavior and record preservation"

echo "PASS: explicit Web apply installation contract tests"
