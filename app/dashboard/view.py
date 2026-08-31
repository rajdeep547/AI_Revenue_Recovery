"""GET /metrics -- the read-only dashboard route.

Two sources, rendered side by side, NEVER summed:

* LIVE       -- from ``WEBHOOK_DB_PATH`` opened read-only (:mod:`app.dashboard.live`)
* CORPUS RUN -- verbatim from ``results/final_run.json`` (:mod:`app.dashboard.corpus`),
                badged "simulated outcomes · eval harness · not live traffic"

The route opens SQLite only through :func:`app.dashboard.live.connect_ro`
(``file:...?mode=ro``) and writes nothing. A missing database or a missing
corpus file both still return HTTP 200.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.dashboard.corpus import load_corpus
from app.dashboard.live import DASH, load_live
from app.dashboard.served import banner, served_db_path

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# One plain CSS file, inlined into the page at render time -- no static mount,
# no <link>, no CDN. Read once at import.
_CSS = (_TEMPLATES_DIR / "metrics.css").read_text(encoding="utf-8")

router = APIRouter()


def _default_db_path() -> str:
    """The file the dashboard reads: DASHBOARD_DB_PATH if set, else the live
    WEBHOOK_DB_PATH (resolved the same way app/db.py does)."""
    return served_db_path()


@router.get("/metrics", response_class=HTMLResponse)
def metrics(request: Request) -> HTMLResponse:
    db_path = _default_db_path()
    context = {
        "dash": DASH,
        "css": _CSS,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "live": load_live(db_path),
        "corpus": load_corpus(),
        "corpus_banner": banner(),
    }
    return _templates.TemplateResponse(
        request=request, name="metrics.html", context=context
    )
