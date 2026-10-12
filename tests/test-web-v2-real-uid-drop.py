#!/usr/bin/env python3
"""Actual Linux privilege-drop probe, run only on isolated ubuntu-latest CI.

The test does not launch runners or mutate any host service. Its own
subprocess permanently drops root to nobody and verifies capabilities and
that regaining UID 0 is impossible.
"""
from __future__ import annotations
import os
import pathlib
import pwd
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
from v2_cli_launcher import drop_identity

if os.geteuid() != 0:
    raise SystemExit("root-only probe must be executed through CI sudo")
target = pwd.getpwnam("nobody")
if target.pw_uid <= 0 or target.pw_gid <= 0:
    raise SystemExit("invalid non-privileged fixture identity")

drop_identity(target.pw_uid, target.pw_gid)
assert os.getresuid() == (target.pw_uid,) * 3
assert os.getresgid() == (target.pw_gid,) * 3
assert os.getgroups() == []

with open("/proc/self/status", encoding="ascii") as stream:
    fields = {k: v.strip() for k, sep, v in
              (line.partition(":") for line in stream if ":" in line)}
for cap in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
    assert int(fields[cap], 16) == 0, cap
assert "1" in fields["NoNewPrivs"].split(), fields["NoNewPrivs"]

try:
    os.seteuid(0)
except PermissionError:
    pass
else:
    raise SystemExit("SECURITY FAILURE: root could be regained")

print("PASS: real permanent UID/GID drop; all capability sets empty; no root regain")
