#!/usr/bin/env bash
if [[ "$*" == "-p 1 -o comm=" ]]; then
  echo systemd
  exit 0
fi
exec /usr/bin/ps "$@"
