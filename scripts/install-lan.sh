#!/usr/bin/env bash
# Shared fresh-installer LAN address selection. Source this file.

grt_private_ipv4() {
  local ipaddr="$1" second
  case "$ipaddr" in
    10.*|192.168.*) return 0 ;;
    172.*)
      second="${ipaddr#172.}"; second="${second%%.*}"
      [[ "$second" =~ ^[0-9]+$ ]] && (( second >= 16 && second <= 31 ))
      return ;;
  esac
  return 1
}

grt_select_lan_address() {
  local default_if count choice selection ipaddr
  local -a candidates preferred
  default_if="$(ip -4 route show default 2>/dev/null |
    awk 'NR==1 {for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
  mapfile -t candidates < <(
    ip -4 -o addr show scope global |
      awk '{print $2, $4}' |
      while read -r iface cidr; do
        ipaddr="${cidr%%/*}"
        if grt_private_ipv4 "$ipaddr"; then
          printf '%s %s\n' "$iface" "$ipaddr"
        fi
      done
  )
  count="${#candidates[@]}"
  if (( count == 0 )); then
    echo "no_private_lan_ipv4" >&2
    return 1
  fi
  preferred=()
  if [[ -n "$default_if" ]]; then
    for choice in "${candidates[@]}"; do
      [[ "${choice%% *}" == "$default_if" ]] && preferred+=("$choice")
    done
  fi
  if (( ${#preferred[@]} == 1 )); then
    printf '%s\n' "${preferred[0]#* }"
    return 0
  fi
  if (( count == 1 )); then
    printf '%s\n' "${candidates[0]#* }"
    return 0
  fi
  if [[ ! -r /dev/tty || ! -w /dev/tty ]]; then
    echo "ambiguous_lan_ipv4_requires_tty" >&2
    return 1
  fi
  echo "Multiple private LAN addresses are available:" >/dev/tty
  local n=1
  for choice in "${candidates[@]}"; do
    printf '  %d) %s\n' "$n" "$choice" >/dev/tty
    ((n++))
  done
  printf 'Select LAN address number: ' >/dev/tty
  read -r selection </dev/tty
  if [[ ! "$selection" =~ ^[0-9]+$ ]] ||
     (( selection < 1 || selection > count )); then
    echo "invalid_lan_selection" >&2
    return 1
  fi
  printf '%s\n' "${candidates[selection-1]#* }"
}
