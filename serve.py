"""Supervise three small processes within one free Render web service."""
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

def main():
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    root=Path(__file__).parent; stopping=False
    children={}; next_start={}; crashes={}
    commands={"web":[sys.executable,"app.py"],"scanner":[sys.executable,"worker.py","scanner"],"commands":[sys.executable,"worker.py","commands"]}
    def shutdown(*args):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,shutdown); signal.signal(signal.SIGINT,shutdown)
    try:
        while not stopping:
            for name,command in commands.items():
                child=children.get(name)
                if child and child.poll() is not None:
                    logging.error("%s process exited (%s)",name,child.returncode)
                    children.pop(name); crashes[name]=min(crashes.get(name,0)+1,5); next_start[name]=time.monotonic()+2**crashes[name]
                if name not in children and time.monotonic()>=next_start.get(name,0):
                    children[name]=subprocess.Popen(command,cwd=root); logging.info("Started %s process",name)
            time.sleep(.5)
    finally:
        for child in children.values():
            if child.poll() is None: child.terminate()
        deadline=time.monotonic()+20
        for child in children.values():
            try: child.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired: child.kill(); child.wait()

if __name__=="__main__": main()
