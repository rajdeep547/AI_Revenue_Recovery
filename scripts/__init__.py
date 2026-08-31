# Makes ``scripts/`` a regular package, like ``app/`` and ``eval/``.
#
# Without this file ``scripts`` is an implicit namespace package. On Linux CI
# that makes ``from scripts import make_demo_db`` fail at collection time
# ("cannot import name 'make_demo_db' from 'scripts' (unknown location)")
# while the submodule form and Windows both happen to resolve it. Regular
# package = deterministic ``from scripts import <module>`` on every platform,
# given the repo root is on sys.path (conftest.py guarantees that).
#
# The individual scripts remain runnable as files (``python scripts/foo.py``):
# each one does its own ``sys.path.insert(0, str(REPO))`` for that case.
