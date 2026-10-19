"""Apply db/migrations/*.sql in order.

    python db/apply.py "host=... dbname=... user=..."         every file, in order (the first time, and for tests)
    python db/apply.py --pending                              only what this database has not got (DATABASE_URL); what the host runs on every deploy
    python db/apply.py --pending --first-run                  the same, on a database that has never had the WhatsApp migrations
    python db/apply.py --status                               what is applied and what is waiting; changes nothing

Each file is idempotent and runs as one implicit transaction (a multi-statement
simple query is atomic in Postgres), so a failure leaves the database exactly
as it was. There is deliberately no BEGIN/COMMIT inside the files: they must
behave the same pasted into the Supabase SQL editor as run from here.

Applied files are recorded in wa_schema_migrations with the file's hash. Plain
`apply` ignores that table (re-running is always safe). `--pending` uses it: a file
is skipped only when its name AND hash are recorded, so an edited file is applied
again.

0001-0003 change the live CRM tables (db/README.md). So `--pending` refuses to be the
one that applies them for the first time unless it is told `--first-run`: read
preflight.sql first, then run once with that flag, then let every deploy run plain
`--pending`. Two deploys at once cannot run it twice (an advisory lock).
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
FIRST = "0001_tenancy"
LOCK_KEY = "prism-wa-migrations"
LOCK_TIMEOUT = "60s"                 # a migration waits this long for a table another writer holds, then fails cleanly instead of queueing everyone behind it

_TRACK = """
create table if not exists wa_schema_migrations (
  name       text primary key,
  sha256     text not null,
  applied_at timestamptz not null default now()
)
"""


class FirstRunRefused(RuntimeError):
    """This database has never had the WhatsApp migrations and nobody said `--first-run`."""


def migration_files() -> list:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def _sha(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _record(cur, path: Path, sql: str) -> None:
    cur.execute(_TRACK)
    cur.execute(
        "insert into wa_schema_migrations (name, sha256) values (%s, %s) "
        "on conflict (name) do update set sha256 = excluded.sha256, "
        "applied_at = now()",
        (path.stem, _sha(sql)))


def apply_all(conn, upto: str = "") -> list:
    """Apply every migration (or those up to and including `upto`, a file
    stem such as '0002_identity'). Returns the names applied."""
    applied = []
    for path in migration_files():
        sql = path.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
            _record(cur, path, sql)
        conn.commit()
        applied.append(path.stem)
        if upto and path.stem == upto:
            break
    return applied


def recorded(conn) -> dict:
    """{name: sha256} of what this database says it has had applied ({} if it has never recorded anything)."""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.wa_schema_migrations')")
        if cur.fetchone()[0] is None:
            conn.rollback()
            return {}
        cur.execute("select name, sha256 from wa_schema_migrations")
        out = {n: h for n, h in cur.fetchall()}
    conn.rollback()
    return out


def pending(conn) -> list:
    """The migration files this database has not got (or has an older version of), in order."""
    have = recorded(conn)
    return [p for p in migration_files() if have.get(p.stem) != _sha(p.read_text(encoding="utf-8"))]


def apply_pending(conn, *, first_run: bool = False) -> list:
    """Apply only what is missing. Returns the names applied (empty when the database is current)."""
    with conn.cursor() as cur:
        cur.execute("select pg_advisory_lock(hashtext(%s))", (LOCK_KEY,))
    conn.commit()
    try:
        todo = pending(conn)
        if not todo:
            return []
        if FIRST not in recorded(conn) and not first_run:
            raise FirstRunRefused(
                "this database has never had the WhatsApp migrations recorded. 0001-0003 change the live CRM tables: read db/README.md, run "
                "db/preflight.sql, then run this once with --first-run (set WA_FIRST_RUN=yes for a deploy) and leave it off afterwards.")
        done = []
        for path in todo:
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute("set lock_timeout = '%s'" % LOCK_TIMEOUT)
                cur.execute(sql)
                _record(cur, path, sql)
                cur.execute("reset lock_timeout")
            conn.commit()
            done.append(path.stem)
        return done
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_unlock(hashtext(%s))", (LOCK_KEY,))
        conn.commit()


def main(argv: list) -> int:
    args = list(argv[1:])
    flags = {a for a in args if a.startswith("--")}
    rest = [a for a in args if not a.startswith("--")]
    unknown = flags - {"--pending", "--first-run", "--status"}
    if unknown or len(rest) > 1 or (not rest and not flags & {"--pending", "--status"}):
        print(__doc__)
        return 2
    dsn = rest[0] if rest else os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL is not set")
        return 2
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        if "--status" in flags:
            todo = {p.stem for p in pending(conn)}
            for p in migration_files():
                print(("waiting  " if p.stem in todo else "applied  ") + p.stem)
            return 0
        if "--pending" in flags:
            first = "--first-run" in flags or (os.environ.get("WA_FIRST_RUN") or "").strip().lower() in ("yes", "1", "true")
            try:
                done = apply_pending(conn, first_run=first)
            except FirstRunRefused as e:
                print("refusing: %s" % e)
                return 1
            for name in done:
                print("applied", name)
            print("database is current" if not done else "%d migration(s) applied" % len(done))
            return 0
        for name in apply_all(conn):
            print("applied", name)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
