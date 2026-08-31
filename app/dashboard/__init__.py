"""Slice 12 · Screen 1 -- the read-only metrics dashboard.

HARD RULES for everything under this package:

* STRICTLY READ-ONLY. The only way this package may touch SQLite is
  ``sqlite3.connect("file:<path>?mode=ro", uri=True)``. No INSERT / UPDATE /
  DELETE, no DDL, no migrations, no writes to any file on disk.
* The synthetic per-customer truth file under ``data/`` is quarantined.
  Nothing here reads it, and nothing here imports from the ``eval`` package
  (which may read it). Recovery is taken only from ``events.status`` in the
  live database.
* No npm, no build step, no CDN. HTML is server-rendered from jinja2
  templates in ``app/dashboard/templates/``; all styling is one plain CSS
  file in that same directory, inlined at render time.

The dashboard never modifies the webhook handler, the policy, or the eval
package -- it only reads what the decision path already wrote to
``webhook_events.db``.
"""
