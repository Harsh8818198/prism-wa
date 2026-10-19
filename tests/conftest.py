"""Fixtures for the SQL tests: one throwaway cluster per run, one template
database holding the legacy schema, and a fresh copy of it per test."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

try:
    import psycopg2
    import psycopg2.extras
    psycopg2.extras.register_uuid()      # uuid and uuid[] come back as UUIDs
except ImportError:                                     # pragma: no cover
    psycopg2 = None

import apply as migrations                              # noqa: E402
import pg                                               # noqa: E402

SKIP_WHY = ("no Postgres available: set PG_BIN=<dir with initdb/pg_ctl> or "
            "PRISM_WA_TEST_ADMIN_DSN=<superuser dsn of a throwaway server> "
            "and `pip install -r requirements-dev.txt`")


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Db:
    """One test's database. `admin` is a superuser autocommit connection."""

    def __init__(self, admin_dsn_for, name: str):
        self.name = name
        self.dsn = admin_dsn_for(name)
        self._conns = []
        self.admin = self.connect(autocommit=True)

    def connect(self, *, autocommit: bool = False, role: str = "",
                tenant: str = ""):
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = autocommit
        self._conns.append(conn)
        with conn.cursor() as cur:
            if role:
                cur.execute("set role " + role)
            if tenant:
                cur.execute("select set_config('app.tenant_id', %s, false)", (tenant,))
        return conn

    def q(self, sql: str, params=None, conn=None) -> list:
        """Run on the admin connection (or `conn`) and return dict rows."""
        c = conn or self.admin
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall()) if cur.description else []

    def one(self, sql: str, params=None, conn=None):
        rows = self.q(sql, params, conn)
        return next(iter(rows[0].values())) if rows else None

    def apply(self, upto: str = "") -> list:
        c = self.connect()
        try:
            return migrations.apply_all(c, upto=upto)
        finally:
            c.close()

    def close(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:                           # noqa: BLE001
                pass


@pytest.fixture(scope="session")
def admin_dsn_for():
    if psycopg2 is None:
        pytest.skip(SKIP_WHY)
    env_dsn = os.environ.get("PRISM_WA_TEST_ADMIN_DSN", "").strip()
    cluster = None
    if env_dsn:
        pg.check_dsn_is_safe(env_dsn)
        base = env_dsn

        def dsn_for(name: str) -> str:
            parts = [p for p in base.split() if not p.startswith("dbname=")]
            return " ".join(parts + ["dbname=" + name])
    else:
        bin_dir = pg.find_bin_dir()
        if bin_dir is None:
            pytest.skip(SKIP_WHY)
        cluster = pg.Cluster(bin_dir).start()
        dsn_for = cluster.admin_dsn
    yield dsn_for
    if cluster:
        cluster.stop()


def _create_db(admin_dsn_for, name: str, template: str = ""):
    conn = psycopg2.connect(admin_dsn_for("postgres"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute('create database "%s"%s' % (
            name, ' template "%s"' % template if template else ""))
    conn.close()


def _drop_db(admin_dsn_for, name: str):
    assert name.startswith(pg.DB_PREFIX), name
    conn = psycopg2.connect(admin_dsn_for("postgres"))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute('drop database if exists "%s" with (force)' % name)
    conn.close()


def _build_template(admin_dsn_for, name: str, *, with_spine_columns: bool):
    _create_db(admin_dsn_for, name)
    conn = psycopg2.connect(admin_dsn_for(name))
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(_read("supabase_stubs.sql"))
        cur.execute(_read("legacy_schema.sql"))
        cur.execute(_read("legacy_spine.sql"))
        if with_spine_columns:
            cur.execute(_read("legacy_spine_columns.sql"))
    conn.close()


@pytest.fixture(scope="session")
def templates(admin_dsn_for):
    names = {"live": pg.DB_PREFIX + "tmpl_live_" + uuid.uuid4().hex[:6],
             "pre": pg.DB_PREFIX + "tmpl_pre_" + uuid.uuid4().hex[:6]}
    _build_template(admin_dsn_for, names["live"], with_spine_columns=True)
    _build_template(admin_dsn_for, names["pre"], with_spine_columns=False)
    yield names
    for n in names.values():
        _drop_db(admin_dsn_for, n)


@pytest.fixture
def make_db(admin_dsn_for, templates):
    """make_db('live'|'pre') -> a fresh database holding the legacy schema."""
    made = []

    def factory(kind: str = "live") -> Db:
        name = pg.DB_PREFIX + uuid.uuid4().hex[:10]
        _create_db(admin_dsn_for, name, templates[kind])
        db = Db(admin_dsn_for, name)
        made.append(db)
        return db

    yield factory
    for db in made:
        db.close()
        _drop_db(admin_dsn_for, db.name)


@pytest.fixture
def legacy(make_db):
    return make_db("live")


@pytest.fixture
def migrated(make_db):
    db = make_db("live")
    db.apply()
    return db
