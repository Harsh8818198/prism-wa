"""Routes for the contact book: the Contacts screen and the chat profile.

    contacts   list  get  journey  create  update  block  delete  tags/set  imports  imports/get
    tags       list  create  rename  delete
    attributes list  define  delete  set_global

Same conventions as ops.py: `POST /internal/<area>/<action>`, a handler is a plain function of (cursor, tenant, body).
"""
from __future__ import annotations

from prism_wa import consent, contact_book as book
from prism_wa import inbox
from prism_wa.ops_common import jsonable, managers_only, need


def _actor(cur, t, d):
    if not d.get("by_agent_id"):
        return None
    try:
        a = inbox._agent(cur, t, d["by_agent_id"])
    except inbox.InboxError:
        return None
    return {"id": a["id"], "name": a["name"]}


def _masked(cur, t, d) -> bool:
    if not d.get("by_agent_id"):
        return False
    try:
        a = inbox._agent(cur, t, d["by_agent_id"])
    except inbox.InboxError:
        return False
    return bool(a["masked"] and a["role"] == "agent")


def _targets(cur, t, d) -> list:
    ids = book.ids_for(cur, t, ids=d.get("contact_ids"), filter_=d.get("filter"))
    if not ids:
        raise ValueError("choose at least one person")
    return ids


def contacts_list(cur, t, d):
    out = book.list_contacts(cur, t, filter_=d.get("filter"), search=d.get("search") or "", sort=d.get("sort") or "created",
                             direction=d.get("direction") or "desc", limit=int(d.get("limit") or 50), offset=int(d.get("offset") or 0),
                             masked=_masked(cur, t, d))
    return jsonable(out)


def contacts_get(cur, t, d):
    need(d, "contact_id")
    return {"contact": jsonable(book.get_profile(cur, t, d["contact_id"], masked=_masked(cur, t, d)))}


def contacts_journey(cur, t, d):
    need(d, "contact_id")
    return {"journey": jsonable(book.journey(cur, t, d["contact_id"], limit=int(d.get("limit") or 100)))}


def contacts_create(cur, t, d):
    need(d, "phone")
    return book.create_contact(cur, t, name=d.get("name") or "", phone=d["phone"], email=d.get("email") or "", tags=d.get("tags") or (),
                               attributes=d.get("attributes"), source=d.get("source") or "MANUAL",
                               attest_consent=bool(d.get("attest_consent")), evidence=d.get("evidence") or "", actor=_actor(cur, t, d))


def contacts_update(cur, t, d):
    need(d, "contact_id", "changes")
    return {"contact": jsonable(book.update_contact(cur, t, d["contact_id"], changes=d["changes"], actor=_actor(cur, t, d)))}


def contacts_block(cur, t, d):
    managers_only(cur, t, d, "block people")
    return {"changed": book.set_blocked(cur, t, _targets(cur, t, d), bool(d.get("blocked", True)), _actor(cur, t, d))}


def contacts_delete(cur, t, d):
    managers_only(cur, t, d, "delete contacts")
    return {"deleted": book.erase(cur, t, _targets(cur, t, d))}


def contacts_tags_set(cur, t, d):
    if not (d.get("add") or d.get("remove")):
        raise ValueError("say which tags to add or remove")
    return book.set_tags(cur, t, _targets(cur, t, d), add=d.get("add") or (), remove=d.get("remove") or (), actor=_actor(cur, t, d))


def contacts_consent(cur, t, d):
    """Record that a person opted in or out by hand (the ledger keeps who said so and why)."""
    need(d, "contact_id", "action")
    if d["action"] not in ("opt_in", "opt_out"):
        raise ValueError("action must be opt_in or opt_out")
    book._contact(cur, t, d["contact_id"])
    who = (_actor(cur, t, d) or {}).get("name") or "a teammate"
    changed = consent.record(cur, t, d["contact_id"], d["action"], "manual", (d.get("evidence") or "set by %s" % who)[:500], by_person=True)
    book._event(cur, t, d["contact_id"], d["action"], _actor(cur, t, d), {"source": "manual"})
    cur.execute("select opted_in, opt_out from contacts where id = %s", (d["contact_id"],))
    opted_in, opted_out = cur.fetchone()
    return {"changed": bool(changed), "opted_in": opted_in, "opted_out": opted_out}


def contacts_imports(cur, t, d):
    return {"imports": jsonable(book.list_imports(cur, t))}


def contacts_imports_get(cur, t, d):
    need(d, "import_id")
    got = book.import_report(cur, t, d["import_id"])
    return {"report": jsonable(got["report"]), "file_name": got["file_name"], "at": jsonable(got["at"]),
            "failures_csv": book.failures_csv(got["report"])}


def tags_list(cur, t, d):
    return {"tags": book.list_tags(cur, t)}


def tags_create(cur, t, d):
    need(d, "name")
    managers_only(cur, t, d, "make tags")
    return {"name": book.create_tag(cur, t, d["name"], d.get("color"))}


def tags_rename(cur, t, d):
    need(d, "old", "new")
    managers_only(cur, t, d, "rename tags")
    return {"changed": book.rename_tag(cur, t, d["old"], d["new"])}


def tags_delete(cur, t, d):
    need(d, "name")
    managers_only(cur, t, d, "delete tags")
    return {"removed_from": book.delete_tag(cur, t, d["name"])}


def attributes_list(cur, t, d):
    return {"attributes": jsonable(book.list_attributes(cur, t))}


def attributes_define(cur, t, d):
    need(d, "key")
    managers_only(cur, t, d, "make attributes")
    return {"attribute": jsonable(book.define_attribute(cur, t, d["key"], d.get("type") or "string", is_global=bool(d.get("global")),
                                                        options=d.get("options")))}


def attributes_delete(cur, t, d):
    need(d, "key")
    managers_only(cur, t, d, "delete attributes")
    return {"deleted": book.delete_attribute(cur, t, d["key"])}


def attributes_set_global(cur, t, d):
    need(d, "key", "value")
    managers_only(cur, t, d, "change workspace-wide values")
    book.set_global_value(cur, t, d["key"], d["value"])
    return {}


OPS = {
    "/internal/contacts/list": contacts_list, "/internal/contacts/get": contacts_get, "/internal/contacts/journey": contacts_journey,
    "/internal/contacts/create": contacts_create, "/internal/contacts/update": contacts_update,
    "/internal/contacts/block": contacts_block, "/internal/contacts/delete": contacts_delete,
    "/internal/contacts/tags/set": contacts_tags_set, "/internal/contacts/consent": contacts_consent,
    "/internal/contacts/imports": contacts_imports, "/internal/contacts/imports/get": contacts_imports_get,
    "/internal/tags/list": tags_list, "/internal/tags/create": tags_create, "/internal/tags/rename": tags_rename,
    "/internal/tags/delete": tags_delete,
    "/internal/attributes/list": attributes_list, "/internal/attributes/define": attributes_define,
    "/internal/attributes/delete": attributes_delete, "/internal/attributes/set_global": attributes_set_global,
}
