#!/usr/bin/env python3
"""Fake official runner config for no-secret PTY lifecycle integration tests."""
import os
import sys

if len(sys.argv) != 2 or sys.argv[1] not in ("create", "remove"):
    sys.exit(62)
if any("fixture-only-token" in argument for argument in sys.argv):
    sys.exit(63)
sys.stdout.write("Enter the registration/removal token: ")
sys.stdout.flush()
value = sys.stdin.readline().rstrip("\r\n")
if value != "fixture-only-token":
    sys.exit(64)
sys.stdout.write("Official CLI fixture operation completed\n")
sys.stdout.flush()
