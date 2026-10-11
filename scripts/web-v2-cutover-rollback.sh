#!/usr/bin/env bash
# Source from setup-web-management-v2.sh; lifecycle state is read-only.
grt_v2_same_lifecycle_state() {
  sudo /usr/bin/python3 - "$STATE_DIR" "$BACKUP/lifecycle-snapshot" <<'PY'
import hashlib, os, stat, sys
state, backup = sys.argv[1:]
folders = ("active-instances", "runner-attestations", "unit-attestations", "create-state", "remove-state")
def digest_tree(base, sub):
    root = os.path.join(base, sub)
    if not os.path.lexists(root): return None
    entries = {}
    def visit(path, rel):
        st = os.lstat(path)
        attrs = (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))
        if stat.S_ISDIR(st.st_mode):
            entries[rel] = ("dir", attrs)
            for name in sorted(os.listdir(path)):
                visit(os.path.join(path, name), rel + "/" + name)
        elif stat.S_ISREG(st.st_mode):
            h = hashlib.sha256()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""): h.update(chunk)
            entries[rel] = ("file", attrs, h.hexdigest())
        else: raise ValueError("unexpected lifecycle entry type")
    visit(root, sub)
    return entries
try:
    if any(digest_tree(state, f) != digest_tree(backup, f) for f in folders): raise SystemExit(1)
except (OSError, ValueError): raise SystemExit(1)
PY
}
grt_v2_restore_v1_services() {
  sudo systemctl stop github-runner-tools-web.service github-runner-tools-executor.service 2>/dev/null || :
  sudo systemctl disable github-runner-tools-executor.service 2>/dev/null || :
  sudo rm -f -- "$EXEC_UNIT" || return 1
  sudo rm -rf -- "$INSTALL_ROOT" || return 1
  sudo cp -a -- "$BACKUP/installed-web" "$INSTALL_ROOT" || return 1
  if sudo test -d "$BACKUP/installed-cli"; then
    sudo rm -rf -- "$CLI_ROOT" || return 1
    sudo cp -a -- "$BACKUP/installed-cli" "$CLI_ROOT" || return 1
  elif sudo test -f "$BACKUP/cli-root-did-not-exist"; then
    sudo rm -rf -- "$CLI_ROOT" || return 1
  else
    echo "Incomplete CLI backup; manual reconciliation required" >&2; return 1
  fi
  sudo cp -a -- "$BACKUP/web.conf" "$CONFIG" || return 1
  sudo cp -a -- "$BACKUP/web-auth.conf" "$AUTH" || return 1
  sudo cp -a -- "$BACKUP/github-runner-tools-web.service" "$WEB_UNIT" || return 1
  sudo systemctl daemon-reload || return 1
  if [[ "$V1_DISPATCH_ENABLED" == 1 ]]; then
    sudo systemctl enable github-runner-tools-dispatch.service || return 1
  else sudo systemctl disable github-runner-tools-dispatch.service || return 1; fi
  if [[ "$V1_WEB_ENABLED" == 1 ]]; then
    sudo systemctl enable github-runner-tools-web.service || return 1
  else sudo systemctl disable github-runner-tools-web.service || return 1; fi
  if [[ "$V1_DISPATCH_ACTIVE" == 1 ]]; then
    sudo systemctl start github-runner-tools-dispatch.service || return 1
  fi
  if [[ "$V1_WEB_ACTIVE" == 1 ]]; then
    sudo systemctl start github-runner-tools-web.service || return 1
  fi
  if [[ "$V1_DISPATCH_ACTIVE" == 1 ]]; then
    sudo systemctl is-active --quiet github-runner-tools-dispatch.service || return 1
  fi
  if [[ "$V1_WEB_ACTIVE" == 1 ]]; then
    sudo systemctl is-active --quiet github-runner-tools-web.service || return 1
  fi
}
grt_v2_rollback_on_exit() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ "${CUTOVER_STARTED:-0}" != 1 || "${CUTOVER_COMMITTED:-0}" == 1 ]]; then return "$status"; fi
  echo "V2 cutover failed; checking rollback compatibility" >&2
  if ! grt_v2_same_lifecycle_state; then
    echo "BLOCKED: changed/unverifiable lifecycle state; stopping V2, requiring manual reconciliation" >&2
    sudo systemctl stop github-runner-tools-web.service github-runner-tools-executor.service 2>/dev/null || :
    return 1
  fi
  if ! grt_v2_restore_v1_services; then
    echo "BLOCKED: automatic V1 restoration failed; manual reconciliation required" >&2
    return 1
  fi
  echo "V1 Web restored; lifecycle state unchanged" >&2
  return "$status"
}
