#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
command -v docker >/dev/null 2>&1 || { echo "FAIL: docker is required" >&2; exit 1; }

TMP="$(mktemp -d)"
IMAGE="grt-fresh-debian13-fixture:$$"
PAYLOAD="$TMP/payload"
DIST="$TMP/dist"
RELEASE="v9.9.7"
PASSWORD="FixturePass123!"
containers=()
images=("$IMAGE")

cleanup() {
  local name img
  for name in "${containers[@]:-}"; do docker rm -f "$name" >/dev/null 2>&1 || true; done
  for img in "${images[@]:-}"; do docker image rm -f "$img" >/dev/null 2>&1 || true; done
  rm -rf -- "$TMP"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

bash "$ROOT/scripts/build-release.sh" "$RELEASE" "$DIST" >/dev/null
ARCHIVE="$DIST/github-runner-tools-$RELEASE.tar.gz"
SHA="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
mkdir -p "$PAYLOAD"
tar -xzf "$ARCHIVE" -C "$PAYLOAD"
[[ ! -e "$PAYLOAD/.git" ]] || fail "release payload unexpectedly contains .git"

docker build -q -t "$IMAGE" -f - "$ROOT" >/dev/null <<'DOCKERFILE'
FROM debian:13
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends       sudo python3 jq rsync curl ca-certificates iproute2 procps util-linux passwd       findutils coreutils grep sed mawk tar gzip &&     rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash runner && useradd -m -s /bin/bash other &&     printf 'runner ALL=(ALL) NOPASSWD:ALL\nother ALL=(ALL) NOPASSWD:ALL\n' >/etc/sudoers.d/grt-fixture &&     chmod 0440 /etc/sudoers.d/grt-fixture
COPY tests/fixtures/fresh-install-systemctl.sh /usr/local/sbin/systemctl
COPY tests/fixtures/fresh-install-ps.sh /usr/local/bin/ps
COPY tests/fixtures/fresh-install-apt-get.sh /usr/local/sbin/apt-get
RUN chmod 0755 /usr/local/sbin/systemctl /usr/local/bin/ps /usr/local/sbin/apt-get
DOCKERFILE

new_container() {
  local name="$1" image="${2:-$IMAGE}"
  docker run -d --name "$name" --hostname grt-fixture     -v "$PAYLOAD:/opt/grt-payload:ro"     "$image" /bin/bash -lc 'while :; do sleep 3600; done' >/dev/null
  containers+=("$name")
}

run_installer() {
  local name="$1" user="$2" release="$3" sha="$4" fault="${5:-}"
  local fault_env=""
  if [[ -n "$fault" ]]; then
    fault_env="GRT_TEST_ALLOW_FAULTS=1 GRT_TEST_FAULT_STAGE=$fault"
  fi
  printf '%s\n%s\n' "$PASSWORD" "$PASSWORD" |
    docker exec -i "$name" /usr/bin/script -q -e -c       "/usr/sbin/runuser -u $user -- env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin GRT_RELEASE_VERSION=$release GRT_PAYLOAD_SHA256=$sha $fault_env bash /opt/grt-payload/scripts/fresh-install.sh"       /dev/null
}

run_expect_failure() {
  local output rc
  set +e
  output="$(run_installer "$@" 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -ne 0 ]] || { printf '%s\n' "$output" >&2; fail "installer unexpectedly succeeded"; }
  printf '%s' "$output"
}

assert_no_runner_mutation() {
  local name="$1"
  docker exec "$name" bash -ceu '
    ! compgen -G "/etc/systemd/system/actions.runner.*.service" >/dev/null
    ! compgen -G "/home/runner/actions-runner-*" >/dev/null
    for d in active-instances create-state remove-state runner-attestations unit-attestations; do
      [[ ! -e "/var/lib/github-runner-tools/$d" ]]
    done
  '
}

assert_complete_install() {
  local name="$1"
  [[ "$(docker exec "$name" jq -r .stage /var/lib/github-runner-tools/install-state.json)" == "INSTALL_COMPLETE" ]] ||
    fail "$name did not reach INSTALL_COMPLETE"
  docker exec "$name" bash -ceu '
    [[ "$(stat -c "%U:%G:%a:%F" /var/lib/github-runner-tools/install-state.json)" == "root:root:600:regular file" ]]
    [[ "$(stat -c "%U:%G:%a:%F" /etc/systemd/system/github-runner-tools-executor.service)" == "root:root:644:regular file" ]]
    [[ "$(stat -c "%U:%G:%a:%F" /etc/systemd/system/github-runner-tools-web.service)" == "root:root:644:regular file" ]]
    [[ ! -e /etc/systemd/system/github-runner-tools-dispatch.service ]]
    systemctl is-active --quiet github-runner-tools-executor.service
    systemctl is-active --quiet github-runner-tools-web.service
    systemctl is-enabled --quiet github-runner-tools-executor.service
    systemctl is-enabled --quiet github-runner-tools-web.service
    [[ -S /run/github-runner-tools/executor.sock ]]
    grep -q '"'"'"event":"operation_exit"'"'"'.*'"'"'"op":"status"'"'"'.*'"'"'"exit_code":0'"'"' /var/log/github-runner-tools/operational.jsonl
  '
  assert_no_runner_mutation "$name"
}

# Identity boundary: root and sudo-root entrypoints fail before persistent mutation.
name="grt-root-boundary-$$"
new_container "$name"
for mode in direct sudo pipe; do
  set +e
  case "$mode" in
    direct)
      out="$(docker exec "$name" env GRT_RELEASE_VERSION="$RELEASE" GRT_PAYLOAD_SHA256="$SHA"         bash /opt/grt-payload/scripts/fresh-install.sh 2>&1)"
      ;;
    sudo)
      out="$(docker exec -u runner "$name" sudo env GRT_RELEASE_VERSION="$RELEASE" GRT_PAYLOAD_SHA256="$SHA"         bash /opt/grt-payload/scripts/fresh-install.sh 2>&1)"
      ;;
    pipe)
      out="$(cat "$PAYLOAD/scripts/fresh-install.sh" | docker exec -i -u runner "$name" sudo bash 2>&1)"
      ;;
  esac
  rc=$?
  set -e
  [[ "$rc" -ne 0 && "$out" == *root_invocation_forbidden* ]] || {
    printf '%s\n' "$out" >&2; fail "root boundary failed for $mode"; }
  docker exec "$name" bash -ceu '
    [[ ! -e /var/lib/github-runner-tools ]]
    [[ ! -e /usr/local/lib/github-runner-tools ]]
    [[ ! -e /etc/github-runner-tools ]]
    [[ ! -e /var/log/github-runner-tools ]]
    [[ ! -e /etc/systemd/system/github-runner-tools-executor.service ]]
    [[ ! -e /etc/systemd/system/github-runner-tools-web.service ]]
    ! getent passwd grt-web >/dev/null
  '
done
docker rm -f "$name" >/dev/null
containers=("${containers[@]/$name}")

# Unknown partial installation without an attempt record is never adopted.
name="grt-unknown-partial-$$"
new_container "$name"
docker exec "$name" mkdir -p /usr/local/lib/github-runner-tools/unknown
out="$(run_expect_failure "$name" runner "$RELEASE" "$SHA")"
[[ "$out" == *untracked_partial_installation* ]] || fail "unknown partial state was not rejected"
docker rm -f "$name" >/dev/null
containers=("${containers[@]/$name}")

stages=(
  RUNTIME_INSTALLED
  AUTHORITY_STATE_INITIALIZED
  ARCHIVE_PLATFORM_INITIALIZED
  EXECUTOR_INSTALLED
  WEB_INSTALLED
  SERVICES_STARTED
  SMOKE_VERIFIED
)

representative=""
for stage in "${stages[@]}"; do
  safe="${stage,,}"
  name="grt-${safe//_/-}-$$"
  new_container "$name"

  out="$(run_expect_failure "$name" runner "$RELEASE" "$SHA" "$stage")"
  [[ "$out" == *"stage=$stage"* && "$out" == *"reason=injected_fault"* ]] || {
    printf '%s\n' "$out" >&2; fail "$stage did not report exact injected stage"; }
  [[ "$(docker exec "$name" jq -r .stage /var/lib/github-runner-tools/install-state.json)" == "$stage" ]] ||
    fail "$stage did not persist last completed stage"
  assert_no_runner_mutation "$name"

  # Every partial stage is release, payload and invoking-user bound.
  out="$(run_expect_failure "$name" runner "v9.9.6" "$SHA")"
  [[ "$out" == *partial_release_mismatch* ]] || fail "$stage release mismatch not rejected"
  out="$(run_expect_failure "$name" runner "$RELEASE" "$(printf '0%.0s' {1..64})")"
  [[ "$out" == *partial_payload_mismatch* ]] || fail "$stage payload mismatch not rejected"
  out="$(run_expect_failure "$name" other "$RELEASE" "$SHA")"
  [[ "$out" == *partial_runner_user_mismatch* ]] || fail "$stage user mismatch not rejected"

  # Clone the partial filesystem, corrupt one committed runtime object, and
  # prove retry refuses to overwrite it.
  corrupt_image="grt-fresh-corrupt-${safe}:$$"
  images+=("$corrupt_image")
  docker commit "$name" "$corrupt_image" >/dev/null
  corrupt_name="${name}-corrupt"
  new_container "$corrupt_name" "$corrupt_image"
  docker exec "$corrupt_name" bash -c 'printf "\n# fixture corruption\n" >> /usr/local/lib/github-runner-tools/cli/status-runners.sh'
  out="$(run_expect_failure "$corrupt_name" runner "$RELEASE" "$SHA")"
  [[ "$out" == *managed_file_content_mismatch* ]] || {
    printf '%s\n' "$out" >&2; fail "$stage corruption was not fail-closed"; }
  docker rm -f "$corrupt_name" >/dev/null
  containers=("${containers[@]/$corrupt_name}")

  # Original exact partial state deterministically resumes to completion.
  run_installer "$name" runner "$RELEASE" "$SHA" >/dev/null
  assert_complete_install "$name"

  if [[ -z "$representative" ]]; then
    representative="$name"
  else
    docker rm -f "$name" >/dev/null
    containers=("${containers[@]/$name}")
  fi
done

# Real LAN HTTP check on one completed fixture.
bind="$(docker exec "$representative" sed -n 's/^WEB_BIND_ADDRESS=//p' /etc/github-runner-tools/web.conf)"
[[ "$bind" =~ ^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.) ]] || fail "installer did not persist RFC1918 bind"

headers="$(docker exec "$representative" curl --noproxy '*' -sS -D - -o /dev/null   --data-urlencode "password=$PASSWORD" "http://$bind:8765/login")"
[[ "$headers" == *"303"* ]] || fail "LAN HTTP login did not redirect after success"
cookie_line="$(printf '%s\n' "$headers" | tr -d '\r' | grep -i '^Set-Cookie:' | head -n1)"
[[ "$cookie_line" == *"HttpOnly"* && "$cookie_line" == *"SameSite=Strict"* ]] ||
  fail "HTTP cookie lost required attributes"
[[ "$cookie_line" != *"Secure"* ]] || fail "HTTP cookie incorrectly contains Secure"

# A new private address must not cause automatic rebinding.
docker exec "$representative" ip addr add 10.254.254.254/32 dev lo
before="$(docker exec "$representative" sha256sum /etc/github-runner-tools/web.conf | awk '{print $1}')"
docker exec "$representative" systemctl restart github-runner-tools-web.service
after="$(docker exec "$representative" sha256sum /etc/github-runner-tools/web.conf | awk '{print $1}')"
[[ "$before" == "$after" ]] || fail "Web config changed during restart"
[[ "$(docker exec "$representative" sed -n 's/^WEB_BIND_ADDRESS=//p' /etc/github-runner-tools/web.conf)" == "$bind" ]] ||
  fail "Web silently rebound after another private address appeared"

# If the persisted address disappears, restart must fail clearly rather than
# selecting the newly-added private address.
cidr="$(docker exec "$representative" ip -4 -o addr show | awk -v ip="$bind" '$4 ~ ("^" ip "/") {print $4; exit}')"
iface="$(docker exec "$representative" ip -4 -o addr show | awk -v ip="$bind" '$4 ~ ("^" ip "/") {print $2; exit}')"
[[ -n "$cidr" && -n "$iface" ]] || fail "could not locate persisted fixture address"
docker exec "$representative" ip addr del "$cidr" dev "$iface"
set +e
restart_out="$(docker exec "$representative" systemctl restart github-runner-tools-web.service 2>&1)"
restart_rc=$?
set -e
[[ "$restart_rc" -ne 0 ]] || fail "Web restart succeeded after persisted address disappeared"
log="$(docker exec "$representative" cat /run/grt-fake-systemctl/github-runner-tools-web.service.log 2>/dev/null || true)"
[[ "$log" == *"configured WEB_BIND_ADDRESS is not assigned to this host"* ]] || {
  printf '%s\n%s\n' "$restart_out" "$log" >&2
  fail "missing clear persisted-address startup failure"
}

echo "PASS: Debian 13 root-isolated fresh install, retry, and LAN HTTP acceptance"
