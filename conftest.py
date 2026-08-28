import os
import sys

# Make the repo root importable no matter how pytest is invoked, so tests can
# `import datagen` and `from app... import ...` the same way.
sys.path.insert(0, os.path.dirname(__file__))
