#!/usr/bin/env python3
"""Run the whole suite without extra PYTHONPATH fiddling.

Usage:  python3 run_tests.py [-v]
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

if __name__ == "__main__":
    verbosity = 2 if "-v" in sys.argv else 1
    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(ROOT, "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
