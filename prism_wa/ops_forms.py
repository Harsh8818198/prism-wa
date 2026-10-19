"""Routes for forms (WhatsApp Flows).

    forms   templates  list  get  create  update  duplicate  delete  check  preview  publish  retire  sync  send  responses  export  key_status  upload_key  endpoint

`endpoint` is not for the app: it is how Meta's live requests for a form reach the code (the licence server passes the raw body and the signature through, and gives Meta
back the text it returns, or the status in an error). Everything else is for owners and managers to build and publish, and for the whole team to send and read.
"""
from __future__ import annotations

import os
import uuid
from typing import Iterable

from prism_wa import form_endpoint, forms, plans, send
from prism_wa.graph import Graph
from prism_wa.ops_common import jsonable, managers_only, need
from prism_wa.vault import Vault


def make_conn_ops(graph: Graph, vault: Vault, app_id: str = "", app_secrets: Iterable[str] = ()) -> dict:
    secrets = tuple(s for s in app_secrets if s) or tuple(s for s in [os.environ.get("META_APP_SECRET", "")] if s)

    def need_vault():
        if vault is None:
            raise LookupError("this is not set up on this server (no token key)")

    def gate(cur, t):
        plans.require(cur, t, "forms", "Forms")

    def templates(conn, t, d):
        return {"templates": forms.template_list()}

    def f_list(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            return {"forms": jsonable(forms.list_forms(cur, t))}

    def f_get(conn, t, d):
        need(d, "form_id")
        with conn.cursor() as cur:
            gate(cur, t)
            return {"form": jsonable(forms.get_form(cur, t, d["form_id"]))}

    def f_create(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "build forms")
            return {"form": jsonable(forms.create_form(cur, t, d, agent_id=d.get("by_agent_id")))}

    def f_update(conn, t, d):
        need(d, "form_id")
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "change forms")
            return {"form": jsonable(forms.update_form(cur, t, d["form_id"], d))}

    def f_duplicate(conn, t, d):
        need(d, "form_id")
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "build forms")
            return {"form": jsonable(forms.duplicate_form(cur, t, d["form_id"], agent_id=d.get("by_agent_id")))}

    def f_delete(conn, t, d):
        need(d, "form_id")
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "remove forms")
            return {"deleted": forms.delete_form(cur, t, d["form_id"], graph=graph, vault=vault)}

    def f_check(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            if d.get("form_id"):
                f = forms._row(cur, t, d["form_id"])
                if "screens" in d:
                    f["screens"] = forms.clean_screens(d["screens"])
                if "endpoint" in d:
                    f["endpoint"] = bool(d["endpoint"])
                if "settings" in d:
                    f["settings"] = forms.clean_settings(d["settings"])
                if "name" in d:
                    f["name"] = d["name"]
            else:
                f = {"name": d.get("name") or "x", "screens": forms.clean_screens(d.get("screens") or []), "endpoint": bool(d.get("endpoint")), "mapping": d.get("mapping") or {},
                     "settings": forms.clean_settings(d.get("settings")), "categories": d.get("categories") or ["LEAD_GENERATION"]}
            return {"problems": forms.check(f)}

    def f_preview(conn, t, d):
        need(d, "form_id")
        with conn.cursor() as cur:
            gate(cur, t)
            return {"flow_json": forms.to_flow_json(forms._row(cur, t, d["form_id"]))}

    def f_publish(conn, t, d):
        need_vault()
        need(d, "form_id")
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "publish forms")
            try:
                return {"form": jsonable(forms.publish(cur, t, graph, vault, d["form_id"], public_url=d.get("public_url") or os.environ.get("PUBLIC_URL") or ""))}
            except forms.FormError:
                conn.commit()                                       # keep what Meta said was wrong, to show beside the form
                raise

    def f_retire(conn, t, d):
        need_vault()
        need(d, "form_id")
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "retire forms")
            return {"form": jsonable(forms.retire(cur, t, graph, vault, d["form_id"]))}

    def f_sync(conn, t, d):
        need_vault()
        with conn.cursor() as cur:
            gate(cur, t)
            return {"changed": forms.sync_status(cur, t, graph, vault)}

    def f_send(conn, t, d):
        need(d, "form_id", "contact_id")
        with conn.cursor() as cur:
            gate(cur, t)
            oid = forms.send_form(cur, t, d["form_id"], contact_id=d["contact_id"], body=d.get("body") or "", button=d.get("button") or "", header=d.get("header") or "",
                                  footer=d.get("footer") or "", idem_key=d.get("idem_key") or "form:%s" % uuid.uuid4().hex, number_id=d.get("number_id"),
                                  sender="agent" if d.get("by_agent_id") else "bot")
            send.attribute(cur, oid, d.get("by_agent_id"))
            return {"queued": True}

    def f_responses(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            return jsonable(forms.list_responses(cur, t, d.get("form_id") or None, limit=d.get("limit") or 100, offset=d.get("offset") or 0))

    def f_export(conn, t, d):
        need(d, "form_id")
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "download answers")
            return {"csv": forms.export_csv(cur, t, d["form_id"])}

    def f_key_status(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            return jsonable(forms.key_status(cur, t))

    def f_upload_key(conn, t, d):
        need_vault()
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "send Meta the form key")
            return forms.upload_key(cur, t, graph, vault)

    def f_endpoint(conn, t, d):
        need_vault()
        need(d, "form_id", "body", "signature")
        with conn.cursor() as cur:
            return {"response": form_endpoint.handle(cur, t, d["form_id"], vault, d["body"].encode("utf-8"), d["signature"], secrets)}

    return {"/internal/forms/templates": templates, "/internal/forms/list": f_list, "/internal/forms/get": f_get, "/internal/forms/create": f_create,
            "/internal/forms/update": f_update, "/internal/forms/duplicate": f_duplicate, "/internal/forms/delete": f_delete, "/internal/forms/check": f_check,
            "/internal/forms/preview": f_preview, "/internal/forms/publish": f_publish, "/internal/forms/retire": f_retire, "/internal/forms/sync": f_sync,
            "/internal/forms/send": f_send, "/internal/forms/responses": f_responses, "/internal/forms/export": f_export, "/internal/forms/key_status": f_key_status,
            "/internal/forms/upload_key": f_upload_key, "/internal/forms/endpoint": f_endpoint}
