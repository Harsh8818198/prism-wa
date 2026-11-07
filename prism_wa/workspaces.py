"""Making a customer's workspace (a `tenants` row) on first use, so nobody has to run SQL for each customer.

The licence server asks for one the first time a licence that holds the WhatsApp add-on opens the WhatsApp tab and has no workspace yet. It names the
workspace by the licence's own id, so asking twice (two seats opening at once, a retry after a timeout) gives the same workspace back and never a second one.

Only the licence server can ask (the shared internal token), and what it sends is the licence id it already checked. Nothing a seat types reaches here.
"""
from __future__ import annotations

import re

_SLUG_PREFIX = "lic-"
_VALID = re.compile(r"^[a-z0-9][a-z0-9-]{5,63}$")


class WorkspaceError(ValueError):
    """The request is not one that can make a workspace."""


def slug_for(external_id: str) -> str:
    """`lic-<licence id>`, lower case, letters, digits and dashes only. Refuses anything too short or odd to be a licence id."""
    cleaned = re.sub(r"[^a-z0-9-]", "", str(external_id or "").strip().lower())
    if not _VALID.match(cleaned):
        raise WorkspaceError("external_id is not a licence id")
    return (_SLUG_PREFIX + cleaned)[:64]


def ensure(cur, external_id: str, name: str = "") -> tuple:
    """(tenant id, created). The workspace for this licence: the one that exists, or a new one."""
    slug = slug_for(external_id)
    label = " ".join(str(name or "").split())[:120] or slug
    cur.execute("insert into tenants (slug, name) values (%s, %s) on conflict (slug) do nothing returning id", (slug, label))
    row = cur.fetchone()
    if row is not None:
        return str(row[0]), True
    cur.execute("select id from tenants where slug = %s", (slug,))
    return str(cur.fetchone()[0]), False
