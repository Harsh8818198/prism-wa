"""Routes for automatic messages (birthdays, reminders, drips).

    autos      list  get  create  update  delete  set_enabled  preview  enrol  log
"""
from __future__ import annotations

from prism_wa import autos
from prism_wa.ops_common import jsonable, managers_only, need


def list_(cur, t, d):
    return {"autos": [jsonable(a) for a in autos.list_autos(cur, t)]}


def get(cur, t, d):
    need(d, "auto_id")
    return {"auto": jsonable(autos.get(cur, t, d["auto_id"]))}


def create(cur, t, d):
    need(d, "kind", "name", "config")
    managers_only(cur, t, d, "make automatic messages")
    return {"auto_id": str(autos.create(cur, t, d["kind"], d["name"], d["config"], created_by=d.get("by_agent_id")))}


def update(cur, t, d):
    need(d, "auto_id")
    managers_only(cur, t, d, "change automatic messages")
    return {"auto": jsonable(autos.update(cur, t, d["auto_id"], name=d.get("name"), config=d.get("config")))}


def delete(cur, t, d):
    need(d, "auto_id")
    managers_only(cur, t, d, "delete automatic messages")
    return {"deleted": autos.delete(cur, t, d["auto_id"])}


def set_enabled(cur, t, d):
    need(d, "auto_id")
    managers_only(cur, t, d, "switch automatic messages on or off")
    return {"auto": jsonable(autos.set_enabled(cur, t, d["auto_id"], bool(d.get("enabled"))))}


def preview(cur, t, d):
    need(d, "config")
    return jsonable(autos.preview(cur, t, d["config"], days=int(d.get("days") or 7)))


def enrol(cur, t, d):
    need(d, "auto_id", "audience")
    managers_only(cur, t, d, "add people to a drip")
    return autos.enrol(cur, t, d["auto_id"], d["audience"])


def log_(cur, t, d):
    need(d, "auto_id")
    return {"log": [jsonable(r) for r in autos.recent(cur, t, d["auto_id"], int(d.get("limit") or 50))], "stats": jsonable(autos.stats(cur, t, d["auto_id"]))}


OPS = {"/internal/autos/list": list_, "/internal/autos/get": get, "/internal/autos/create": create, "/internal/autos/update": update, "/internal/autos/delete": delete,
       "/internal/autos/set_enabled": set_enabled, "/internal/autos/preview": preview, "/internal/autos/enrol": enrol, "/internal/autos/log": log_}
