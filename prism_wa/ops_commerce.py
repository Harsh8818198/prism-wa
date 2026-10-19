"""Routes for the shop: the catalogue, orders and payments.

    commerce   config/get  config/save
    catalog    list  save  delete  import  pull  push  catalogs  visibility  set_visibility  send
    orders     list  get  create  update  status  cancel
    payments   request  list  get  check  refund  dashboard  retarget  razorpay_webhook

Reading and changing settings, pushing to Meta and refunding are for owners and managers; making orders and asking for payment are for anyone on the team.
"""
from __future__ import annotations

import os
import uuid

from prism_wa import commerce, payments, plans, send
from prism_wa.graph import Graph
from prism_wa.ops_common import jsonable, managers_only, need
from prism_wa.vault import Vault


def make_conn_ops(graph: Graph, vault: Vault, app_id: str = "") -> dict:
    def need_vault():
        if vault is None:
            raise LookupError("this is not set up on this server (no token key)")

    def gate(cur, t):
        plans.require(cur, t, "commerce", "Commerce")

    def cfg_get(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            cfg = commerce.get_config(cur, t)
            base = (d.get("public_url") or os.environ.get("PUBLIC_URL") or "").rstrip("/")
            cfg["webhook_url"] = ("%s/v1/whatsapp/pay/razorpay/%s" % (base, t)) if base else ""
            return {"config": jsonable(cfg)}

    def cfg_save(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "change the shop settings")
            cfg = commerce.save_config(cur, t, vault, d)
            base = (d.get("public_url") or os.environ.get("PUBLIC_URL") or "").rstrip("/")
            cfg["webhook_url"] = ("%s/v1/whatsapp/pay/razorpay/%s" % (base, t)) if base else ""
            return {"config": jsonable(cfg)}

    def cat_list(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            out = commerce.list_products(cur, t, q=d.get("q") or "", category=d.get("category") or "", available=d.get("available"), limit=d.get("limit") or 200,
                                         offset=d.get("offset") or 0)
            return jsonable(out)

    def cat_save(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "change the catalogue")
            return {"product": jsonable(commerce.save_product(cur, t, d))}

    def cat_delete(conn, t, d):
        need(d, "product_id")
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "change the catalogue")
            return {"deleted": commerce.delete_product(cur, t, d["product_id"])}

    def cat_import(conn, t, d):
        need(d, "csv")
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "change the catalogue")
            return commerce.import_csv(cur, t, d["csv"])

    def cat_pull(conn, t, d):
        need_vault()
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "read the Meta catalogue")
            return commerce.pull_catalog(cur, t, graph, vault)

    def cat_push(conn, t, d):
        need_vault()
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "change the Meta catalogue")
            return commerce.push_catalog(cur, t, graph, vault)

    def cat_catalogs(conn, t, d):
        need_vault()
        with conn.cursor() as cur:
            gate(cur, t)
            token, waba, _pnid = commerce._token_for(cur, t, vault)
            return {"catalogs": graph.waba_catalogs(token, waba)}

    def cat_visibility(conn, t, d):
        need_vault()
        with conn.cursor() as cur:
            gate(cur, t)
            token, _waba, pnid = commerce._token_for(cur, t, vault)
            s = graph.commerce_settings(token, pnid)
            return {"catalog_visible": bool(s.get("is_catalog_visible")), "cart_enabled": bool(s.get("is_cart_enabled"))}

    def cat_set_visibility(conn, t, d):
        need_vault()
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "change what customers see in the chat")
            token, _waba, pnid = commerce._token_for(cur, t, vault)
            graph.set_commerce_settings(token, pnid, catalog_visible=d.get("catalog_visible"), cart_enabled=d.get("cart_enabled"))
            s = graph.commerce_settings(token, pnid)
            return {"catalog_visible": bool(s.get("is_catalog_visible")), "cart_enabled": bool(s.get("is_cart_enabled"))}

    def cat_send(conn, t, d):
        need(d, "contact_id", "kind")
        key = d.get("idem_key") or "shop:%s" % uuid.uuid4().hex
        with conn.cursor() as cur:
            gate(cur, t)
            common = dict(contact_id=d["contact_id"], idem_key=key, sender="agent" if d.get("by_agent_id") else "bot", number_id=d.get("number_id"))
            if d["kind"] == "product":
                need(d, "retailer_id")
                oid = commerce.send_product(cur, t, retailer_id=d["retailer_id"], body=d.get("body") or "", footer=d.get("footer") or "", **common)
            elif d["kind"] == "products":
                need(d, "sections")
                oid = commerce.send_products(cur, t, sections=d["sections"], header=d.get("header") or "Our products", body=d.get("body") or "", footer=d.get("footer") or "", **common)
            elif d["kind"] == "catalog":
                oid = commerce.send_catalog(cur, t, body=d.get("body") or "", footer=d.get("footer") or "", thumbnail_retailer_id=d.get("thumbnail") or "", **common)
            else:
                raise ValueError("send a product, a list of products, or the catalogue")
            send.attribute(cur, oid, d.get("by_agent_id"))
            return {"queued": True}

    def o_list(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            return jsonable(commerce.list_orders(cur, t, status=d.get("status") or "", q=d.get("q") or "", contact_id=d.get("contact_id"), limit=d.get("limit") or 100,
                                                 offset=d.get("offset") or 0))

    def o_get(conn, t, d):
        need(d, "order_id")
        with conn.cursor() as cur:
            gate(cur, t)
            return {"order": jsonable(commerce.get_order(cur, t, d["order_id"]))}

    def o_create(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            return {"order": jsonable(commerce.create_order(cur, t, d, agent_id=d.get("by_agent_id"), source="agent" if d.get("by_agent_id") else "manual"))}

    def o_update(conn, t, d):
        need(d, "order_id")
        with conn.cursor() as cur:
            gate(cur, t)
            return {"order": jsonable(commerce.update_order(cur, t, d["order_id"], d))}

    def o_status(conn, t, d):
        need(d, "order_id", "status")
        with conn.cursor() as cur:
            gate(cur, t)
            return {"order": jsonable(commerce.set_status(cur, t, d["order_id"], d["status"], note=d.get("note") or "", notify=d.get("notify") is not False,
                                                          sender="agent" if d.get("by_agent_id") else "system"))}

    def o_cancel(conn, t, d):
        need(d, "order_id")
        with conn.cursor() as cur:
            gate(cur, t)
            return {"order": jsonable(commerce.cancel(cur, t, d["order_id"], vault=vault))}

    def p_request(conn, t, d):
        need_vault()
        need(d, "order_id")
        with conn.cursor() as cur:
            gate(cur, t)
            return {"payment": jsonable(payments.request_payment(cur, t, vault, order_id=d["order_id"], agent_id=d.get("by_agent_id"), send_message=d.get("send") is not False))}

    def p_list(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            return jsonable(payments.list_payments(cur, t, status=d.get("status") or "", q=d.get("q") or "", since=d.get("since") or None, until=d.get("until") or None,
                                                   limit=d.get("limit") or 100, offset=d.get("offset") or 0))

    def p_get(conn, t, d):
        need(d, "payment_id")
        with conn.cursor() as cur:
            gate(cur, t)
            return {"payment": jsonable(payments.get_payment(cur, t, d["payment_id"]))}

    def p_check(conn, t, d):
        need_vault()
        need(d, "payment_id")
        with conn.cursor() as cur:
            gate(cur, t)
            return {"payment": jsonable(payments.check(cur, t, vault, graph, d["payment_id"]))}

    def p_refund(conn, t, d):
        need_vault()
        need(d, "payment_id")
        with conn.cursor() as cur:
            gate(cur, t)
            managers_only(cur, t, d, "give money back")
            amount = commerce.paise(d["amount"]) if d.get("amount") not in (None, "") else None
            return {"payment": jsonable(payments.refund(cur, t, vault, payment_id=d["payment_id"], amount_paise=amount, speed=d.get("speed") or "normal", reason=d.get("reason") or "",
                                                        agent_id=d.get("by_agent_id"), record_only=d.get("record_only") is True))}

    def p_dashboard(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            return jsonable(payments.dashboard(cur, t, d.get("since") or None, d.get("until") or None))

    def p_retarget(conn, t, d):
        with conn.cursor() as cur:
            gate(cur, t)
            return {"people": jsonable(payments.retarget(cur, t, older_than_minutes=int(d.get("older_than_minutes") or 60)))}

    def p_razorpay_webhook(conn, t, d):
        need_vault()
        need(d, "body", "signature")
        with conn.cursor() as cur:
            return payments.razorpay_webhook(cur, t, vault, d["body"].encode("utf-8"), d["signature"])

    return {"/internal/commerce/config/get": cfg_get, "/internal/commerce/config/save": cfg_save,
            "/internal/catalog/list": cat_list, "/internal/catalog/save": cat_save, "/internal/catalog/delete": cat_delete, "/internal/catalog/import": cat_import,
            "/internal/catalog/pull": cat_pull, "/internal/catalog/push": cat_push, "/internal/catalog/catalogs": cat_catalogs, "/internal/catalog/visibility": cat_visibility,
            "/internal/catalog/set_visibility": cat_set_visibility, "/internal/catalog/send": cat_send,
            "/internal/orders/list": o_list, "/internal/orders/get": o_get, "/internal/orders/create": o_create, "/internal/orders/update": o_update,
            "/internal/orders/status": o_status, "/internal/orders/cancel": o_cancel,
            "/internal/payments/request": p_request, "/internal/payments/list": p_list, "/internal/payments/get": p_get, "/internal/payments/check": p_check,
            "/internal/payments/refund": p_refund, "/internal/payments/dashboard": p_dashboard, "/internal/payments/retarget": p_retarget,
            "/internal/payments/razorpay_webhook": p_razorpay_webhook}
