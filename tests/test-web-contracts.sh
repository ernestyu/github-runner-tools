#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
fail() { echo "FAIL: $*" >&2; exit 1; }

TMP="$(mktemp -d)"
cleanup() { rm -rf -- "$TMP"; }
trap cleanup EXIT

# status-runners.sh --json must expose the frozen machine contract without
# scraping human-readable output.
MOCKBIN="$TMP/mockbin"
BASE="$TMP/runners"
mkdir -p "$MOCKBIN" "$BASE"

cat > "$MOCKBIN/systemctl" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "show" ]]; then
  property=""
  for ((i=1; i<=$#; i++)); do
    if [[ "${!i}" == "-p" ]]; then
      j=$((i+1)); property="${!j}"
    fi
  done
  case "$property" in
    LoadState) printf '%s\n' "${MOCK_LOAD_STATE-loaded}" ;;
    ActiveState) printf '%s\n' "${MOCK_ACTIVE_STATE-active}" ;;
    *) printf '%s\n' "" ;;
  esac
  exit 0
fi
exit 0
MOCK
chmod +x "$MOCKBIN/systemctl"

CONFIGURED="$BASE/actions-runner-ernestyu--example"
mkdir -p "$CONFIGURED"
cat > "$CONFIGURED/.runner" <<'JSON'
{"agentName":"local-ci-ernestyu--example","gitHubUrl":"https://github.com/ernestyu/example"}
JSON
printf '%s\n' 'actions.runner.ernestyu-example.local-ci-ernestyu--example.service' > "$CONFIGURED/.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$CONFIGURED/config.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$CONFIGURED/svc.sh"
chmod +x "$CONFIGURED/config.sh" "$CONFIGURED/svc.sh"

JSON_OUT="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '
  length == 1 and
  .[0].repository == "ernestyu/example" and
  .[0].runner_name == "local-ci-ernestyu--example" and
  .[0].configured == true and
  .[0].service_state == "active" and
  .[0].management_state == "configured" and
  .[0].can_remove == true and
  .[0].can_recover_local == false
' <<<"$JSON_OUT" >/dev/null || fail "configured status JSON contract mismatch"

rm -rf -- "$CONFIGURED"
RECOVERABLE="$BASE/actions-runner-ernestyu--example"
mkdir -p "$RECOVERABLE"
printf '%s\n' 'actions.runner.ernestyu-example.custom-runner.service' > "$RECOVERABLE/.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$RECOVERABLE/svc.sh"
chmod +x "$RECOVERABLE/svc.sh"

JSON_OUT="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '
  length == 1 and
  .[0].repository == "ernestyu/example" and
  .[0].runner_name == "custom-runner" and
  .[0].configured == false and
  .[0].management_state == "recoverable_residue" and
  .[0].can_remove == false and
  .[0].can_recover_local == true
' <<<"$JSON_OUT" >/dev/null || fail "recoverable status JSON contract mismatch"

MOCK_ACTIVE_STATE=inactive JSON_OUT="$(PATH="$MOCKBIN:$PATH" MOCK_ACTIVE_STATE=inactive RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].service_state == "inactive" and .[0].can_recover_local == true' <<<"$JSON_OUT" >/dev/null ||
  fail "inactive service status contract mismatch"

MOCK_LOAD_STATE=not-found JSON_OUT="$(PATH="$MOCKBIN:$PATH" MOCK_LOAD_STATE=not-found RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].service_state == "absent" and .[0].can_recover_local == true' <<<"$JSON_OUT" >/dev/null ||
  fail "absent service status contract mismatch"

MOCK_LOAD_STATE="" JSON_OUT="$(PATH="$MOCKBIN:$PATH" MOCK_LOAD_STATE="" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].service_state == "unknown" and .[0].can_recover_local == false' <<<"$JSON_OUT" >/dev/null ||
  fail "unknown service status contract mismatch"

rm -rf -- "$RECOVERABLE"
INCOMPLETE="$BASE/actions-runner-incomplete"
mkdir -p "$INCOMPLETE"
printf '%s\n' partial > "$INCOMPLETE/partial.txt"
JSON_OUT="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].repository == null and .[0].management_state == "incomplete" and .[0].can_remove == false and .[0].can_recover_local == false' <<<"$JSON_OUT" >/dev/null ||
  fail "incomplete status JSON contract mismatch"

rm -rf -- "$INCOMPLETE"
AMBIG="$BASE/actions-runner-ambiguous"
mkdir -p "$AMBIG"
ln -s nowhere "$AMBIG/.runner"
JSON_OUT="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].repository == null and .[0].management_state == "ambiguous" and .[0].can_remove == false and .[0].can_recover_local == false' <<<"$JSON_OUT" >/dev/null ||
  fail "ambiguous status JSON contract mismatch"
rm -rf -- "$AMBIG"

# CLI/Web identity authority must derive the same local id.
SHELL_LOCAL_ID="$(
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  make_local_id "ErnestYu" "HyperGrid"
)"
PY_LOCAL_ID="$(PYTHONPATH="$ROOT/web" python3 -c 'from grt_web_common import make_local_id; print(make_local_id("ErnestYu","HyperGrid"))')"
[[ "$SHELL_LOCAL_ID" == "$PY_LOCAL_ID" ]] || fail "CLI/Web local identity parity mismatch"

# CLI-only scripts must not install or enable Web Management.
for script in register-runner.sh remove-runner.sh status-runners.sh setup-local-archive.sh; do
  if grep -Eq 'setup-web-management|github-runner-tools-web\.service|web-dispatch\.sock|tailscale serve' "$ROOT/scripts/$script"; then
    fail "CLI-only path unexpectedly enables Web Management: $script"
  fi
done

# setup-web-management defaults to dry-run and the mutation branch is after the
# dry-run exit. Exercise it with harmless command mocks and verify no marker was
# touched by mocked privileged commands.
SETUP_MOCK="$TMP/setup-mock"
mkdir -p "$SETUP_MOCK"
MARKER="$TMP/persistent-mutation"
cat > "$SETUP_MOCK/systemctl" <<'MOCK'
#!/usr/bin/env bash
if [[ "${1:-}" == "is-active" && "${2:-}" == "--quiet" && "${3:-}" == "tailscaled" ]]; then exit 0; fi
exit 0
MOCK
cat > "$SETUP_MOCK/tailscale" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
cat > "$SETUP_MOCK/sudo" <<MOCK
#!/usr/bin/env bash
touch "$MARKER"
exit 99
MOCK
chmod +x "$SETUP_MOCK/systemctl" "$SETUP_MOCK/tailscale" "$SETUP_MOCK/sudo"

PATH="$SETUP_MOCK:$PATH" bash "$ROOT/scripts/setup-web-management.sh" --dry-run >/dev/null
[[ ! -e "$MARKER" ]] || fail "Web dry-run invoked privileged persistent mutation"

# Exercise the real OS credential transition used by the Web worker: root
# creates the private socket, child drops all groups/UID/GID to the runner
# owner, and the child must see a root peer on that inherited AF_UNIX socket.
sudo python3 - "$ROOT" "$(id -u)" "$(id -g)" <<'PY' || fail "real root-to-runner UID/GID drop and root-peer contract failed"
import os
import socket
import sys

root, uid_s, gid_s = sys.argv[1:4]
uid, gid = int(uid_s), int(gid_s)
sys.path.insert(0, os.path.join(root, "web"))
import lifecycle_worker

parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
pid = os.fork()
if pid == 0:
    try:
        parent.close()
        lifecycle_worker.drop_to_runner_identity(uid, gid)
        if lifecycle_worker.privileged_peer_uid(child.fileno()) != 0:
            os._exit(3)
        os._exit(0)
    except BaseException:
        os._exit(4)

child.close()
_, status = os.waitpid(pid, 0)
parent.close()
if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
    raise SystemExit(1)
PY

# The installed dispatcher unit must provision the two capabilities required
# solely for the fixed worker identity transition while preserving the frozen
# sandbox.
grep -Fq 'AmbientCapabilities=CAP_SETUID CAP_SETGID' "$ROOT/scripts/setup-web-management.sh" ||
  fail "dispatcher unit does not explicitly provision CAP_SETUID/CAP_SETGID"
grep -Fq 'NoNewPrivileges=true' "$ROOT/scripts/setup-web-management.sh" ||
  fail "dispatcher NoNewPrivileges hardening missing"
grep -Fq 'RestrictSUIDSGID=true' "$ROOT/scripts/setup-web-management.sh" ||
  fail "dispatcher RestrictSUIDSGID hardening missing"

# Real systemd regression: under the production-relevant hardening, explicit
# AmbientCapabilities must yield CAP_SETUID and CAP_SETGID in both permitted
# and effective sets. The transient unit is collected immediately.
sudo systemd-run --quiet --wait --pipe --collect \
  -p 'User=root' \
  -p 'Group=root' \
  -p 'NoNewPrivileges=yes' \
  -p 'RestrictSUIDSGID=yes' \
  -p 'ProtectSystem=strict' \
  -p 'ProtectKernelTunables=yes' \
  -p 'ProtectKernelModules=yes' \
  -p 'ProtectControlGroups=yes' \
  -p 'LockPersonality=yes' \
  -p 'AmbientCapabilities=CAP_SETUID CAP_SETGID' \
  /usr/bin/python3 - <<'PY' ||
  fail "transient systemd capability provisioning regression failed"
status = {}
with open("/proc/self/status", encoding="utf-8") as handle:
    for line in handle:
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()

required = (1 << 6) | (1 << 7)
permitted = int(status["CapPrm"], 16)
effective = int(status["CapEff"], 16)
if permitted & required != required or effective & required != required:
    raise SystemExit(1)
print("DISPATCHER_CAPABILITY_PROVISIONING PASS")
PY

# Reproduce the Debian capability-inheritance failure mechanism with real
# Linux capget/capset, then exercise Dispatcher -> fixed Worker -> list while
# inheritable capabilities and NoNewPrivs are both present in the dispatcher.
sudo python3 - "$ROOT" "$(id -u)" "$(id -g)" <<'PY' || fail "capability inheritance / dispatcher-worker regression failed"
import ctypes
import grp
import json
import os
import pathlib
import pwd
import shutil
import stat
import sys
import tempfile

root, uid_s, gid_s = sys.argv[1:4]
uid, gid = int(uid_s), int(gid_s)
sys.path.insert(0, os.path.join(root, "web"))

import dispatcher
import lifecycle_worker

def set_one_inheritable_capability():
    header, data = lifecycle_worker._capget_data()
    chosen = None
    for word_index, entry in enumerate(data):
        permitted = int(entry.permitted)
        if permitted:
            bit = permitted & -permitted
            entry.inheritable |= bit
            chosen = word_index * 32 + (bit.bit_length() - 1)
            break
    if chosen is None:
        raise RuntimeError("root test process has no permitted capability to place in CapInh")
    lifecycle_worker._capset_data(header, data)
    caps = lifecycle_worker.read_capability_sets()
    if caps["CapInh"] == 0:
        raise RuntimeError("failed to construct nonzero CapInh")
    return chosen, caps

chosen, before = set_one_inheritable_capability()
print(f"CAPABILITY_REGRESSION_PRE CapInh_nonzero=1 selected_cap={chosen}")

# Exact previous drop sequence: setgroups -> setresgid -> setresuid -> strict
# verification. Linux preserves CapInh across this UID transition, so the old
# worker reaches the same identity-verification failure that mapped to exit 71.
pid = os.fork()
if pid == 0:
    try:
        os.setgroups([])
        os.setresgid(gid, gid, gid)
        os.setresuid(uid, uid, uid)
        caps = lifecycle_worker.read_capability_sets()
        failed_strict_verification = not lifecycle_worker.verify_unprivileged_identity(uid, gid)
        if caps["CapInh"] != 0 and failed_strict_verification:
            os._exit(71)
        os._exit(10)
    except BaseException:
        os._exit(11)

_, status = os.waitpid(pid, 0)
if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 71:
    raise RuntimeError(f"legacy drop sequence did not reproduce exit-71 condition: status={status}")
print("CAPABILITY_REGRESSION_LEGACY exit=71 cause=nonzero_CapInh_after_uid_drop")

# Match the production hardening property relevant to this path.
libc = ctypes.CDLL(None, use_errno=True)
PR_SET_NO_NEW_PRIVS = 38
if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
    err = ctypes.get_errno()
    raise OSError(err, "PR_SET_NO_NEW_PRIVS failed")

tmp = pathlib.Path(tempfile.mkdtemp(prefix="grt-cap-int-"))
try:
    os.chmod(tmp, 0o755)
    install = tmp / "web"
    cli = install / "cli"
    install.mkdir(mode=0o755)
    cli.mkdir(mode=0o755)

    for name in ("lifecycle_worker.py", "grt_web_common.py", "pty_token_adapter.py"):
        src = pathlib.Path(root) / "web" / name
        dst = install / name
        shutil.copyfile(src, dst)
        os.chown(dst, 0, 0)
        os.chmod(dst, 0o755 if name != "grt_web_common.py" else 0o644)

    status_script = cli / "status-runners.sh"
    status_script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        "status={}\n"
        "for line in open('/proc/self/status', encoding='utf-8'):\n"
        "    if ':' in line:\n"
        "        k,v=line.split(':',1); status[k]=v.strip()\n"
        "keys=('CapInh','CapPrm','CapEff','CapAmb')\n"
        "caps_zero=all(int(status.get(k,'0'),16)==0 for k in keys)\n"
        f"identity_ok=(os.getuid()=={uid} and os.geteuid()=={uid} and os.getgid()=={gid} and os.getegid()=={gid} and os.getgroups()==[])\n"
        "if not (caps_zero and identity_ok): raise SystemExit(9)\n"
        "print(json.dumps([{'repository':'owner/repo','caps_zero':caps_zero,'identity_ok':identity_ok}]))\n",
        encoding="utf-8",
    )
    os.chown(status_script, 0, 0)
    os.chmod(status_script, 0o755)

    worker_path = str(install / "lifecycle_worker.py")
    pty_path = str(install / "pty_token_adapter.py")
    lock_path = tmp / "mutation.lock"
    lock_path.write_text("", encoding="utf-8")
    os.chown(lock_path, 0, gid)
    os.chmod(lock_path, 0o660)

    pw = pwd.getpwuid(uid)
    gr = grp.getgrgid(gid)
    rt = object.__new__(dispatcher.Runtime)
    rt.runner_user = pw.pw_name
    rt.runner_home = pw.pw_dir
    rt.runner_group = gr.gr_name
    rt.worker_path = worker_path
    rt.cli_dir = str(cli)
    rt.pty_adapter = pty_path
    rt.lock_file = str(lock_path)
    rt.timeout = 10
    rt.runner_pw = pw
    rt.runner_gr = gr

    server = object.__new__(dispatcher.DispatchServer)
    server.runtime = rt
    result = dispatcher.DispatchServer.execute(server, {"op": "list"})
    if result.get("ok") is not True:
        raise RuntimeError(f"dispatcher list failed: {result!r}")
    runners = result.get("runners")
    if not isinstance(runners, list) or len(runners) != 1:
        raise RuntimeError(f"unexpected runner list: {runners!r}")
    if runners[0].get("caps_zero") is not True or runners[0].get("identity_ok") is not True:
        raise RuntimeError(f"worker did not reach strict unprivileged state: {runners!r}")
    print("CAPABILITY_REGRESSION_FIXED dispatcher_list_ok=1 caps_zero=1 identity_ok=1")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY

# The real Web lifecycle code path must invoke the official config.sh through
# the PTY adapter and must not contain a secret --token argument in the Web
# branch. Normal CLI may still use --token because it is outside Web V1.
python3 - "$ROOT" <<'PY' || fail "Web final config.sh consumer contract mismatch"
from pathlib import Path
import sys

root = Path(sys.argv[1])
reg = (root / "scripts/register-runner.sh").read_text()
rem = (root / "scripts/remove-runner.sh").read_text()

reg_start = reg.index('if [[ "$WEB_MODE" == "1" ]]; then', reg.index('echo "==> Registering runner'))
reg_end = reg.index("else", reg_start)
reg_web = reg[reg_start:reg_end]
if '--mode create' not in reg_web or './config.sh' not in reg_web or '--token "$TOKEN"' in reg_web:
    raise SystemExit(1)

rem_start = rem.index('if [[ "$WEB_MODE" == "1" ]]; then', rem.index('echo "==> Removing runner registration'))
rem_end = rem.index("else", rem_start)
rem_web = rem[rem_start:rem_end]
if '--mode remove' not in rem_web or './config.sh remove' not in rem_web or '--token "$TOKEN"' in rem_web:
    raise SystemExit(1)
PY

# Internal Web lifecycle context must be bound to a root Unix peer, not merely
# to caller-controlled environment flags plus a forgeable JSON context reply.
python3 - "$ROOT" <<'PY' || fail "fake same-UID Web registration context was accepted"
import os
import socket
import subprocess
import sys

root = sys.argv[1]
left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    script = f'''
set -Eeuo pipefail
RUNNER_TOOLS_LIB_ONLY=1 source "{root}/scripts/register-runner.sh"
WEB_MODE=1
LOCK_ALREADY_HELD=1
PRIVILEGED_FD={left.fileno()}
acquire_mutation_lock
'''
    proc = subprocess.run(
        ["bash", "-c", script],
        pass_fds=(left.fileno(),),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if os.geteuid() != 0 and proc.returncode == 0:
        raise SystemExit(1)
finally:
    left.close()
    right.close()
PY

python3 - "$ROOT" <<'PY' || fail "fake same-UID Web removal context was accepted"
import os
import socket
import subprocess
import sys

root = sys.argv[1]
left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    script = f'''
set -Eeuo pipefail
RUNNER_TOOLS_LIB_ONLY=1 source "{root}/scripts/remove-runner.sh"
WEB_MODE=1
LOCK_ALREADY_HELD=1
PRIVILEGED_FD={left.fileno()}
acquire_mutation_lock
'''
    proc = subprocess.run(
        ["bash", "-c", script],
        pass_fds=(left.fileno(),),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if os.geteuid() != 0 and proc.returncode == 0:
        raise SystemExit(1)
finally:
    left.close()
    right.close()
PY

# Web setup apply contract must install fixed identities/permissions and remain
# explicit opt-in. These are contract assertions over the installer itself;
# live ownership is separately covered by frozen Debian acceptance.
grep -Fq 'sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$WEB_USER"' "$ROOT/scripts/setup-web-management.sh" ||
  fail "Web apply does not create locked grt-web account"
grep -Fq 'sudo install -o root -g "$WEB_USER" -m 0640 "$TMP_CONFIG" "$CONFIG_FILE"' "$ROOT/scripts/setup-web-management.sh" ||
  fail "Web config ownership contract missing"
grep -Fq 'sudo install -o root -g "$WEB_USER" -m 0640 "$TMP_AUTH" "$AUTH_FILE"' "$ROOT/scripts/setup-web-management.sh" ||
  fail "Web auth ownership contract missing"
grep -Fq 'User=$WEB_USER' "$ROOT/scripts/setup-web-management.sh" ||
  fail "Web service user contract missing"
grep -Fq 'User=root' "$ROOT/scripts/setup-web-management.sh" ||
  fail "dispatcher root service contract missing"
if grep -Eq '^[[:space:]]*(sudo[[:space:]]+)?tailscale[[:space:]]+funnel([[:space:]]|$)' "$ROOT/scripts/setup-web-management.sh"; then
  fail "Web setup contains forbidden executable Tailscale Funnel command"
fi

# Lock/revalidation ordering must remain explicit: CLI acquisition occurs
# before target filesystem validation/mutation in both mutating scripts.
python3 - "$ROOT" <<'PY' || fail "CLI lock/revalidation ordering contract failed"
from pathlib import Path
import sys

root = Path(sys.argv[1])
reg = (root / "scripts/register-runner.sh").read_text()
rem = (root / "scripts/remove-runner.sh").read_text()
for name, text, target in [
    ("register", reg, 'RUNNER_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"'),
    ("remove", rem, 'NEW_DIR="$RUNNER_BASE_DIR/actions-runner-$LOCAL_ID"'),
]:
    lock = text.find("acquire_mutation_lock", text.find("main()"))
    validation = text.find(target, text.find("main()"))
    if lock < 0 or validation < 0 or lock >= validation:
        raise SystemExit(f"{name} does not lock before target validation")
PY

# CLI/Web parity: the status surface and CLI recovery authority must both
# reject a mismatched service scope rather than inventing repository identity.
PARITY_BASE="$TMP/parity-runners"
mkdir -p "$PARITY_BASE/actions-runner-owner--repo"
printf '%s\n' 'actions.runner.other-repo.custom.service' > "$PARITY_BASE/actions-runner-owner--repo/.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$PARITY_BASE/actions-runner-owner--repo/svc.sh"
chmod +x "$PARITY_BASE/actions-runner-owner--repo/svc.sh"
PARITY_JSON="$(PATH="$MOCKBIN:$PATH" RUNNER_BASE_DIR="$PARITY_BASE" bash "$ROOT/scripts/status-runners.sh" --json)"
jq -e '.[0].can_recover_local == false and (.[0].management_state == "ambiguous" or .[0].repository == null)' <<<"$PARITY_JSON" >/dev/null ||
  fail "Web status accepted mismatched recovery service identity"
if (
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  validate_recovery_identity     "$PARITY_BASE/actions-runner-owner--repo"     "$PARITY_BASE/actions-runner-owner--repo"     owner repo
) >/dev/null 2>&1; then
  fail "CLI recovery authority accepted mismatched service identity"
fi

# Authoritative removal/recovery state is revalidated after lock acquisition.
# A stale UI decision must therefore fail before any destructive step.
STALE_BASE="$TMP/stale-runners"
STALE_LOCK="$TMP/stale-lock"
mkdir -p "$STALE_BASE" "$STALE_LOCK"
STALE_DIR="$STALE_BASE/actions-runner-owner--repo"
mkdir -p "$STALE_DIR"
printf '%s\n' 'actions.runner.owner-repo.test-runner.service' > "$STALE_DIR/.service"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STALE_DIR/svc.sh"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STALE_DIR/config.sh"
chmod +x "$STALE_DIR/svc.sh" "$STALE_DIR/config.sh"

# Stale can_remove=true: configured metadata disappeared before mutation.
if (
  export GRT_TEST_MODE=1
  export GRT_TEST_LOCK_DIR="$STALE_LOCK"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  main --base-dir "$STALE_BASE" owner/repo
) >/dev/null 2>&1; then
  fail "stale normal-removal eligibility was not rejected"
fi
[[ -d "$STALE_DIR" ]] || fail "stale normal-removal rejection mutated runner directory"

# Stale can_recover_local=true: .runner appeared before mutation.
printf '%s\n' '{"gitHubUrl":"https://github.com/owner/repo"}' > "$STALE_DIR/.runner"
if (
  export GRT_TEST_MODE=1
  export GRT_TEST_LOCK_DIR="$STALE_LOCK"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  main --base-dir "$STALE_BASE" --recover-local owner/repo
) >/dev/null 2>&1; then
  fail "stale recovery eligibility was not rejected"
fi
[[ -d "$STALE_DIR" && -f "$STALE_DIR/.runner" ]] ||
  fail "stale recovery rejection mutated runner state"

# Core CLI-only lock bootstrap must work without Web setup and fail closed on
# symlink/wrong metadata. Use an isolated temporary path; no Web component is involved.
CORE_LOCK_ROOT="$TMP/core-lock-bootstrap"
(
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  MUTATION_LOCK_DIR="$CORE_LOCK_ROOT"
  MUTATION_LOCK_FILE="$CORE_LOCK_ROOT/mutation.lock"
  ensure_mutation_lock
)
[[ "$(stat -c '%U:%G:%a:%F' "$CORE_LOCK_ROOT")" == "root:root:755:directory" ]] ||
  fail "core lock directory bootstrap contract mismatch"
[[ "$(stat -c '%U:%G:%a:%F' "$CORE_LOCK_ROOT/mutation.lock")" == "root:$(id -gn):660:regular empty file" ||
   "$(stat -c '%U:%G:%a:%F' "$CORE_LOCK_ROOT/mutation.lock")" == "root:$(id -gn):660:regular file" ]] ||
  fail "core lock file bootstrap contract mismatch"

sudo rm -f -- "$CORE_LOCK_ROOT/mutation.lock"
sudo rmdir -- "$CORE_LOCK_ROOT"
ln -s "$TMP" "$CORE_LOCK_ROOT"
if (
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  MUTATION_LOCK_DIR="$CORE_LOCK_ROOT"
  MUTATION_LOCK_FILE="$CORE_LOCK_ROOT/mutation.lock"
  ensure_mutation_lock
) >/dev/null 2>&1; then
  fail "symlinked core lock directory was accepted"
fi
rm -f -- "$CORE_LOCK_ROOT"

sudo install -d -o root -g root -m 0777 "$CORE_LOCK_ROOT"
if (
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  MUTATION_LOCK_DIR="$CORE_LOCK_ROOT"
  MUTATION_LOCK_FILE="$CORE_LOCK_ROOT/mutation.lock"
  ensure_mutation_lock
) >/dev/null 2>&1; then
  fail "wrong-mode core lock directory was accepted"
fi
sudo rm -rf -- "$CORE_LOCK_ROOT"

# Installer authority invariants: Web credentials are root:grt-web 0640,
# installed executable code is root-owned, frontend/dispatcher identities are
# distinct, and no NOPASSWD path is introduced.
grep -Fq 'sudo install -o root -g "$WEB_USER" -m 0640 "$TMP_CONFIG" "$CONFIG_FILE"' "$ROOT/scripts/setup-web-management.sh" ||
  fail "root:grt-web Web config contract missing"
grep -Fq 'sudo install -o root -g "$WEB_USER" -m 0640 "$TMP_AUTH" "$AUTH_FILE"' "$ROOT/scripts/setup-web-management.sh" ||
  fail "root:grt-web auth contract missing"
grep -Fq 'sudo install -o root -g root -m 0755 "$ROOT/web/$file" "$INSTALL_ROOT/$file"' "$ROOT/scripts/setup-web-management.sh" ||
  fail "root-owned installed Web code contract missing"
if grep -Eq 'NOPASSWD|/etc/sudoers|sudoers\.d' "$ROOT/scripts/setup-web-management.sh"; then
  fail "Web setup introduces forbidden passwordless sudo policy"
fi
if grep -Eq 'usermod[[:space:]].*(-aG|-G).*actions|usermod[[:space:]].*(-aG|-G).*grt-web' "$ROOT/scripts/setup-web-management.sh"; then
  fail "Web setup unexpectedly merges runner/Web groups"
fi

# Shared lock: a CLI mutation must fail busy when another process owns the same
# core lock. Test against the removal implementation in isolated test mode.
LOCKDIR="$TMP/shared-lock"
mkdir -p "$LOCKDIR"
: > "$LOCKDIR/mutation.lock"
(
  exec 9<>"$LOCKDIR/mutation.lock"
  flock -x 9
  printf ready > "$TMP/lock-ready"
  sleep 5
) &
HOLDER=$!
for _ in {1..50}; do [[ -f "$TMP/lock-ready" ]] && break; sleep 0.02; done
[[ -f "$TMP/lock-ready" ]] || fail "lock holder did not start"

if (
  export GRT_TEST_MODE=1
  export GRT_TEST_LOCK_DIR="$LOCKDIR"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/register-runner.sh"
  acquire_mutation_lock
) >/dev/null 2>&1; then
  kill "$HOLDER" 2>/dev/null || true
  wait "$HOLDER" 2>/dev/null || true
  fail "register mutation ignored an already-held shared lock"
fi

if (
  export GRT_TEST_MODE=1
  export GRT_TEST_LOCK_DIR="$LOCKDIR"
  RUNNER_TOOLS_LIB_ONLY=1 source "$ROOT/scripts/remove-runner.sh"
  acquire_mutation_lock
) >/dev/null 2>&1; then
  kill "$HOLDER" 2>/dev/null || true
  wait "$HOLDER" 2>/dev/null || true
  fail "CLI mutation ignored an already-held shared lock"
fi
kill "$HOLDER" 2>/dev/null || true
wait "$HOLDER" 2>/dev/null || true

echo "PASS: Web Management shell contract tests"
