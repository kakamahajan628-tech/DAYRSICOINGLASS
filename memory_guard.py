"""Container-aware browser budget. Stops only children created by this capture."""
import gc
import os
from pathlib import Path
import threading
import time
import psutil

MIB=1024*1024


def container_memory():
    """Use the kernel's actual container usage/limit, not host-wide RAM."""
    for current,limit in (("/sys/fs/cgroup/memory.current","/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes","/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            used=int(Path(current).read_text().strip())
            cap=int(Path(limit).read_text().strip())
            if 0<cap<2**60:
                return {"usage_bytes":used,"limit_bytes":cap,"usage_mib":round(used/MIB,1),"limit_mib":round(cap/MIB,1)}
        except (OSError,ValueError): pass
    return {"usage_bytes":None,"limit_bytes":None,"usage_mib":None,"limit_mib":None}


def descendants():
    try: return psutil.Process().children(recursive=True)
    except psutil.Error: return []


class BrowserMemoryGuard:
    def __init__(self, reader=container_memory):
        self.reader=reader
        self.stop_event=threading.Event()
        self.thread=None
        self.baseline=set()
        self.started=0
        self.exceeded=False
        self.peak=0
        self.killed=0
        self.killed_pids=set()
        self.limit=None

    def start(self):
        self.started=time.time()
        self.baseline={p.pid for p in descendants()}
        # Leave headroom for HTTP, database writes, JSON and report generation.
        cap=self.reader().get("limit_bytes")
        self.limit=min(int(cap*.86),440*MIB) if cap else None
        if not self.limit: return self
        self.sample()
        self.thread=threading.Thread(target=self._watch,name="browser-memory-budget",daemon=True)
        self.thread.start()
        return self

    def sample(self):
        usage=self.reader().get("usage_bytes")
        if usage is None: return False
        self.peak=max(self.peak,usage)
        if self.limit and usage>=self.limit:
            self.exceeded=True
            self.kill_capture_children()
            return True
        return False

    def kill_capture_children(self):
        # The web-service Python process and any pre-existing child are excluded.
        # Process creation time also protects against PID reuse.
        children=descendants()
        for process in reversed(children):
            if process.pid in self.baseline: continue
            try:
                if process.create_time()<self.started-.5: continue
                process.kill()
                if process.pid not in self.killed_pids:
                    self.killed_pids.add(process.pid)
                    self.killed+=1
            except psutil.Error: pass

    def _watch(self):
        while not self.stop_event.is_set():
            self.sample()
            # Also catch a new child launched concurrently with the first sample.
            if self.exceeded: self.kill_capture_children()
            self.stop_event.wait(.25)

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=2)
        self.thread=None

    def summary(self):
        return {"guard":"MEMORY_BUDGET_EXCEEDED" if self.exceeded else ("ENABLED_DURING_CAPTURE" if self.limit else "NO_CONTAINER_LIMIT_DETECTED"),
            "container_peak_mib":round(self.peak/MIB,1) if self.peak else None,
            "stop_threshold_mib":round(self.limit/MIB,1) if self.limit else None,"capture_children_stopped":self.killed}


def release_memory():
    gc.collect()
