"""Apply the database migrations this build ships with, when the service starts (WA_MIGRATE_ON_START=yes).

For a host that has no "run before the deploy goes live" step (Render's free web service). The paid blueprint does the same thing with its
pre-deploy command, and `python db/apply.py --pending` does it by hand; all three are the one function in db/apply.py, so they cannot differ.

It applies only what the database lacks, takes the same advisory lock as the other two (two instances starting together cannot both run a
migration), and, like them, REFUSES to be the first to touch a database that has never had the WhatsApp migrations (0001-0003 change the live
CRM tables): run `python db/apply.py --pending --first-run` once by hand, or set WA_FIRST_RUN=yes for exactly one start.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import psycopg2

log = logging.getLogger("prism_wa.migrate")

APPLY_PY = Path(__file__).resolve().parent.parent / "db" / "apply.py"


def _runner():
    spec = importlib.util.spec_from_file_location("prism_wa_db_apply", APPLY_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_on_start(dsn: str, *, first_run: bool = False) -> list:
    """Names of the migrations applied (empty when the database was already current). Raises RuntimeError to stop the start-up."""
    runner = _runner()
    conn = psycopg2.connect(dsn)
    try:
        done = runner.apply_pending(conn, first_run=first_run)
    except runner.FirstRunRefused as e:
        raise RuntimeError("refusing to start: %s" % e) from e
    finally:
        conn.close()
    for name in done:
        log.info("applied migration %s", name)
    return done
