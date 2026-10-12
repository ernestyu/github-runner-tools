#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

cat >"$tmp/ip" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *"route show default"* ]]; then
  printf '%s' "${FAKE_ROUTE:-}"
elif [[ "$*" == *"addr show scope global"* ]]; then
  printf '%s' "${FAKE_ADDRS:-}"
else
  exit 2
fi
EOF
chmod +x "$tmp/ip"
export PATH="$tmp:$PATH"
source "$ROOT/scripts/install-lan.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }\n\nexport FAKE_ROUTE FAKE_ADDRS\n\nFAKE_ROUTE='default via 192.168.1.1 dev eth0
'
FAKE_ADDRS='2: eth0    inet 192.168.1.20/24 brd 192.168.1.255 scope global eth0
'
[[ "$(grt_select_lan_address)" == "192.168.1.20" ]] || fail "single RFC1918 candidate"

FAKE_ROUTE='default via 10.0.0.1 dev eth0
'
FAKE_ADDRS='2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0
3: docker0  inet 172.18.0.1/16 brd 172.18.255.255 scope global docker0
'
[[ "$(grt_select_lan_address)" == "10.0.0.5" ]] || fail "default-route candidate not preferred"

FAKE_ROUTE='default via 203.0.113.1 dev eth0
'
FAKE_ADDRS='2: eth0    inet 203.0.113.7/24 brd 203.0.113.255 scope global eth0
3: lo      inet 127.0.0.1/8 scope global lo
'
if grt_select_lan_address >/dev/null 2>&1; then
  fail "public/loopback-only host was accepted"
fi

FAKE_ROUTE='default via 172.31.0.1 dev eth0
'
FAKE_ADDRS='2: eth0    inet 172.31.9.5/20 brd 172.31.15.255 scope global eth0
'
[[ "$(grt_select_lan_address)" == "172.31.9.5" ]] || fail "172.16/12 candidate rejected"

for bad in 172.15.1.1 172.32.1.1 169.254.1.1 100.64.0.1 0.0.0.0 127.0.0.1; do
  if grt_private_ipv4 "$bad"; then
    fail "non-RFC1918 address accepted: $bad"
  fi
done

grep -Fq 'Select LAN address number:' "$ROOT/scripts/install-lan.sh" ||
  fail "ambiguous candidates do not require explicit selection"

echo "PASS: fresh installer LAN selection contract"
