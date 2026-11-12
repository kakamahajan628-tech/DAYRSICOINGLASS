"""Offline test suite. No bot token, API key or external database required."""
import sys
import unittest
from pathlib import Path

root=Path(__file__).resolve().parent
local_deps=root.parent/".cloud-test-deps"
if local_deps.exists(): sys.path.insert(0,str(local_deps))
sys.path.insert(0,str(root))
suite=unittest.defaultTestLoader.discover(str(root/"tests"))
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
