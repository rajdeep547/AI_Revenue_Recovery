"""Which SQLite file the read-only dashboard serves, and whether it is live.

* ``WEBHOOK_DB_PATH`` -- the live webhook database (the app writes to it) and
  the default the dashboard reads.
* ``DASHBOARD_DB_PATH`` -- when set, points the dashboard at a *different*
  file (e.g. ``data/demo.db``, the corpus run) without redirecting the webhook
  writer.

Any served DB other than the live one raises a persistent, non-dismissible
banner on every dashboard page, so a reader is never one click away from
mistaking corpus data for live traffic.
"""

from __future__ import annotations

import os
from pathlib import Path

CORPUS_BANNER = (
    "Corpus run — decisions from 2,000 synthetic failures through the real "
    "pipeline. Outcomes are simulated. Not live traffic."
)


def live_db_path() -> str:
    return os.environ.get("WEBHOOK_DB_PATH", "webhook_events.db")


def served_db_path() -> str:
    """The file the dashboard actually reads: ``DASHBOARD_DB_PATH`` if set,
    otherwise the live DB."""
    return os.environ.get("DASHBOARD_DB_PATH") or live_db_path()


def is_live_db() -> bool:
    served, live = served_db_path(), live_db_path()
    try:
        return Path(served).resolve() == Path(live).resolve()
    except OSError:
        return served == live


def banner() -> str | None:
    """The corpus banner string when serving anything but the live DB, else
    ``None``."""
    return None if is_live_db() else CORPUS_BANNER
