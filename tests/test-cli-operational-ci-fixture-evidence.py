#!/usr/bin/env python3
"""Assert real V2 CLI fixture runs emitted lifecycle events to one JSONL sink.

Executed AFTER existing full fake-GitHub create/remove/Status fixture suite.
The fixture processes are the actual Bash CLI, not stubbed logger calls.
"""
import json,os,pathlib,sys
root=pathlib.Path(os.environ.get("RUNNER_TEMP","/tmp"))/f"grt-native-logging-{os.getuid()}"
paths=list(root.glob("operational*.jsonl"))
assert paths, "No persisted CLI operational logs from full V2 fixtures"
records=[]
for p in paths:
    records += [json.loads(line) for line in p.read_text().splitlines()]
pairs={(r.get("op"),r.get("event"),r.get("result")) for r in records}
for wanted in [("create","operation","begin"),("create","remote_registration","begin"),
               ("remove","operation","begin"),("remove","remote_removal","begin"),
               ("status","operation_exit","ok")]:
    assert wanted in pairs, f"Actual V2 CLI emitted no {wanted}; found={sorted(pairs)}"
assert not any("token" in str(r).lower() for r in records), "Potential token marker in events"
print(f"PASS: actual V2 CLI fake-GitHub fixtures emitted {len(records)} persisted records across Create/Remove/Status")
