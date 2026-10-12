#!/usr/bin/env python3
"""Unprivileged, no-secret fixture for the Executor FD3/FD4 kernel mapping."""
import hashlib
import json
import os
import socket
import sys

token = os.read(3, 1024)
os.close(3)
if token != b"fixture-only-token\n":
    sys.exit(70)
control = socket.socket(fileno=4)
control.sendall(json.dumps({
    "op": "create_state",
    "repository": "owner/repo",
    "runner_dir": "/home/actions/actions-runner-owner--repo",
    "runner_name": "local-ci-owner--repo",
    "stage": "PRE_REGISTRATION",
}).encode() + b"\n")
reply = bytearray()
while not reply.endswith(b"\n"):
    chunk = control.recv(4096)
    if not chunk:
        sys.exit(71)
    reply.extend(chunk)
if json.loads(reply).get("ok") is not True:
    sys.exit(72)
control.close()
print(hashlib.sha256(token).hexdigest(), flush=True)
