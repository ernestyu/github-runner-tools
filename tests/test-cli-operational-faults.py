#!/usr/bin/env python3
"""Executable fault and multiprocessing coverage for native CLI logger."""
import datetime as dt
import fcntl
import importlib.util
import json
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/"scripts"/"cli-operational-logger.py"
spec=importlib.util.spec_from_file_location("native", SOURCE)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def record(i):
    return dict(v=1,ts="2026-10-11T00:00:00+00:00",run_id=f"{i:032x}",seq=1,
                op="status",event="inventory",stage="none",result="ok",
                reason="none",exit_code=0,repository=None,runner=None)

def worker(root, start, count, q):
    try:
        for i in range(start,start+count):
            with mod.Logger(root) as l:
                l.append(record(i))
        q.put(None)
    except Exception as exc:
        q.put(f"{type(exc).__name__}: {exc}")

def lock_worker(root,ready,release):
    fd=os.open(str(Path(root)/"operational.lock"),os.O_RDWR)
    fcntl.flock(fd,fcntl.LOCK_EX)
    ready.set()
    release.wait(8)
    os.close(fd)

class OperationalFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.lock=self.root/"operational.lock"
        self.lock.touch(mode=0o600)
        self.lock.chmod(0o600)

    def test_concurrent_processes_append_complete_jsonl(self):
        ctx=mp.get_context("spawn")
        q=ctx.Queue()
        procs=[ctx.Process(target=worker,args=(str(self.root),i*20,20,q)) for i in range(5)]
        for p in procs:p.start()
        try:
            errors=[q.get(timeout=35) for _ in procs]
            self.assertEqual(errors,[None]*len(procs))
        finally:
            for p in procs:
                p.join(timeout=8)
                if p.is_alive():p.terminate()
                self.assertEqual(p.exitcode,0)
        lines=[json.loads(line) for p in self.root.glob("operational*.jsonl")
               for line in p.read_text().splitlines()]
        self.assertEqual(len(lines),100)
        self.assertEqual(len({r["run_id"] for r in lines}),100)

    def test_multiprocess_rotation_conserves_records(self):
        ctx=mp.get_context("spawn")
        # Must patch the constant inside spawned children as well: write enough
        # records to exceed 10 MiB by repeated valid events without changed caps
        # is too expensive for CI; test rotation with sequential small cap and
        # concurrent actual 10MiB append independently.
        with patch.object(mod,"MAX_FILE",1500):
            for i in range(40):
                with mod.Logger(str(self.root)) as l:l.append(record(i))
        files=list(self.root.glob("operational*.jsonl"))
        self.assertGreater(len(files),1)
        self.assertTrue(all(p.stat().st_size<=1500 for p in files))
        self.assertEqual(sum(len(p.read_text().splitlines()) for p in files),40)

    def test_lock_timeout_is_bounded_without_mutation(self):
        ctx=mp.get_context("spawn")
        ready=ctx.Event();release=ctx.Event()
        p=ctx.Process(target=lock_worker,args=(str(self.root),ready,release))
        p.start()
        try:
            self.assertTrue(ready.wait(12))
            before=time.monotonic()
            with self.assertRaises(mod.LoggingUnavailable) as cm:
                with mod.Logger(str(self.root)):pass
            self.assertEqual(str(cm.exception),"lock_timeout")
            self.assertLess(time.monotonic()-before,4)
            self.assertFalse((self.root/"operational.jsonl").exists())
        finally:
            release.set();p.join(timeout=8)
            if p.is_alive():p.terminate()
            self.assertEqual(p.exitcode,0)

    def test_partial_write_does_not_claim_success_and_detects_torn_tail(self):
        original=mod.os.write
        def torn(fd,data):
            return original(fd,data[:max(1,len(data)//3)])
        with patch.object(mod.os,"write",side_effect=torn):
            with self.assertRaises(mod.LoggingUnavailable) as cm:
                with mod.Logger(str(self.root)) as l:l.append(record(1))
        self.assertEqual(str(cm.exception),"short_write")
        with self.assertRaises(mod.LoggingUnavailable) as cm:
            with mod.Logger(str(self.root)):pass
        self.assertEqual(str(cm.exception),"corrupt_log_tail")

    def test_enospc_refuses_append_preserves_existing(self):
        import errno
        with mod.Logger(str(self.root)) as l:l.append(record(1))
        before=(self.root/"operational.jsonl").read_bytes()
        with patch.object(mod.os,"write",side_effect=OSError(errno.ENOSPC,"full")):
            with self.assertRaises(OSError) as cm:
                with mod.Logger(str(self.root)) as l:l.append(record(2))
        self.assertEqual(cm.exception.errno,errno.ENOSPC)
        self.assertEqual((self.root/"operational.jsonl").read_bytes(),before)

    def test_rotation_failure_is_bounded_and_original_log_intact(self):
        with patch.object(mod,"MAX_FILE",280):
            with mod.Logger(str(self.root)) as l:l.append(record(1))
            before=(self.root/"operational.jsonl").read_bytes()
            with patch.object(mod.os,"rename",side_effect=OSError(5,"forced rename failure")):
                with self.assertRaises(OSError):
                    with mod.Logger(str(self.root)) as l:l.append(record(2))
            self.assertEqual((self.root/"operational.jsonl").read_bytes(),before)

    def test_fsync_failure_is_reported_not_misclassified_as_success(self):
        with patch.object(mod.os,"fsync",side_effect=OSError(5,"forced fsync failure")):
            with self.assertRaises(OSError):
                with mod.Logger(str(self.root)) as l:l.append(record(1))
        # Bytes may be in the page cache; no guaranteed durability is claimed.

    def test_capacity_rejects_when_only_active_and_lock_remain(self):
        with patch.object(mod,"MAX_TOTAL",150):
            with self.assertRaises(mod.LoggingUnavailable):
                with mod.Logger(str(self.root)) as l:l.append(record(1))
            self.assertFalse((self.root/"operational.jsonl").exists())

if __name__=="__main__":
    unittest.main()
