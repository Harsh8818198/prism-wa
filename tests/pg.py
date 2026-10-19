"""A throwaway Postgres for the SQL tests.

The migrations in db/migrations are the part of this project that can destroy
data, so they are tested against a real Postgres rather than a mock: row-level
security, PL/pgSQL, advisory locks and partial unique indexes behave the way
they do in production or the test proves nothing.

Two ways to get one, in order:

  1. PRISM_WA_TEST_ADMIN_DSN — a superuser DSN for a server you are happy to
     have databases created on (a CI service container, a local install). The
     harness only ever creates and drops databases whose names start with
     `prism_wa_t_`, and refuses any host that is not loopback unless
     PRISM_WA_TEST_ALLOW_REMOTE=1. Never point this at Supabase.
  2. Postgres binaries — PG_BIN=<dir holding initdb/pg_ctl/postgres>, or
     initdb on PATH. A private cluster is initialised in a temp directory on a
     free loopback port and deleted afterwards.

With neither, the SQL tests skip with a message saying how to enable them —
they never pass by not running.
"""
from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
DB_PREFIX = "prism_wa_t_"


def _exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def find_bin_dir() -> Optional[Path]:
    candidates = []
    if os.environ.get("PG_BIN"):
        candidates.append(Path(os.environ["PG_BIN"]))
    found = shutil.which("initdb")
    if found:
        candidates.append(Path(found).parent)
    for c in candidates:
        if (c / _exe("initdb")).is_file() and (c / _exe("pg_ctl")).is_file():
            return c
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cluster:
    """A private Postgres cluster this process started and will delete."""

    def __init__(self, bin_dir: Path):
        self.bin = bin_dir
        self.root = Path(tempfile.mkdtemp(prefix="prism-wa-pg-"))
        self.data = self.root / "data"
        self.log = self.root / "postgres.log"
        self.port = _free_port()
        self.started = False

    def start(self) -> "Cluster":
        subprocess.run(
            [str(self.bin / _exe("initdb")), "-D", str(self.data), "-U", "postgres",
             "-A", "trust", "-E", "UTF8", "--locale=C"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
        opts = ("-p %d -c listen_addresses=127.0.0.1 -c fsync=off "
                "-c synchronous_commit=off -c full_page_writes=off "
                "-c max_connections=60" % self.port)
        # stdout/stderr to DEVNULL and the server's own output to a log file:
        # on Windows a detached postgres inheriting a pipe keeps subprocess
        # waiting for it to close, which never happens.
        subprocess.run(
            [str(self.bin / _exe("pg_ctl")), "-D", str(self.data), "-o", opts,
             "-l", str(self.log), "-w", "start"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=120)
        self.started = True
        atexit.register(self.stop)
        return self

    def stop(self) -> None:
        if self.started:
            subprocess.run(
                [str(self.bin / _exe("pg_ctl")), "-D", str(self.data), "-m",
                 "immediate", "stop"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            self.started = False
        shutil.rmtree(self.root, ignore_errors=True)

    def admin_dsn(self, dbname: str = "postgres") -> str:
        return "host=127.0.0.1 port=%d dbname=%s user=postgres" % (self.port, dbname)


def check_dsn_is_safe(dsn: str) -> None:
    """Refuse an admin DSN that could be somebody's real database."""
    host = ""
    for part in dsn.split():
        if part.startswith("host="):
            host = part[5:]
    if not host and "@" in dsn:
        host = dsn.split("@", 1)[1].split("/", 1)[0].split(":", 1)[0]
    lowered = host.lower()
    if "supabase" in lowered:
        raise RuntimeError("Refusing to run the SQL tests against Supabase.")
    if host and host not in LOOPBACK and os.environ.get("PRISM_WA_TEST_ALLOW_REMOTE") != "1":
        raise RuntimeError(
            "PRISM_WA_TEST_ADMIN_DSN points at %r, which is not loopback. Set "
            "PRISM_WA_TEST_ALLOW_REMOTE=1 only for a server that exists to be "
            "tested on." % host)
