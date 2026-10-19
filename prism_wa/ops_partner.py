"""Routes for the enterprise and reseller side.

    audit        list  export
    sso          config  save  test  start  complete  status
    partner      branding/get  branding/save  branding/effective  invite  redeem  unlink  customers  reseller  set_percent  ledger/add  ledger/list  ledger/paid
    coexistence  numbers  sync  mark

The audit log is read by owners and managers. Single sign-on is changed only by the workspace owner (who is also never locked out by it). `sso/complete` is called by the
licence server when the person comes back from their provider, not by the app.
"""
from __future__ import annotations

from prism_wa import audit, coexistence, inbox, partner, plans, sso
from prism_wa.graph import Graph
from prism_wa.ops_common import jsonable, managers_only, need
from prism_wa.vault import Vault


def _owner_only(cur, t, d, what: str) -> None:
    if d.get("by_agent_id") and inbox._agent(cur, t, d["by_agent_id"])["role"] != "owner":
        raise inbox.InboxError("not_allowed", "only the workspace owner can %s" % what)


def make_conn_ops(graph: Graph, vault: Vault, app_id: str = "") -> dict:
    def need_vault():
        if vault is None:
            raise LookupError("this is not set up on this server (no token key)")

    # audit
    def a_list(conn, t, d):
        with conn.cursor() as cur:
            managers_only(cur, t, d, "read the audit log")
            return jsonable(audit.list_entries(cur, t, action=d.get("action") or "", actor=d.get("actor") or "", q=d.get("q") or "", since=d.get("since") or None,
                                               until=d.get("until") or None, limit=d.get("limit") or 100, offset=d.get("offset") or 0))

    def a_export(conn, t, d):
        with conn.cursor() as cur:
            managers_only(cur, t, d, "download the audit log")
            return {"csv": audit.export_csv(cur, t, d.get("since") or None, d.get("until") or None)}

    # single sign-on
    def s_config(conn, t, d):
        with conn.cursor() as cur:
            managers_only(cur, t, d, "see the sign-in settings")
            plans.require(cur, t, "sso", "Single sign-on")
            return {"config": jsonable(sso.get_config(cur, t))}

    def s_save(conn, t, d):
        with conn.cursor() as cur:
            _owner_only(cur, t, d, "change single sign-on")
            return {"config": jsonable(sso.save_config(cur, t, vault, d))}

    def s_test(conn, t, d):
        with conn.cursor() as cur:
            _owner_only(cur, t, d, "test single sign-on")
            return sso.test(cur, t, vault)

    def s_required(conn, t, d):
        with conn.cursor() as cur:
            return {"required": sso.required(cur, t)}

    def s_start(conn, t, d):
        need(d, "redirect_uri")
        with conn.cursor() as cur:
            plans.require(cur, t, "sso", "Single sign-on")
            return sso.start(cur, t, d["redirect_uri"])

    def s_complete(conn, t, d):
        need_vault()
        need(d, "state", "code")
        with conn.cursor() as cur:
            return sso.complete(cur, t, vault, state=d["state"], code=d["code"])

    def s_status(conn, t, d):
        need(d, "state")
        with conn.cursor() as cur:
            return sso.status(cur, t, d["state"])

    # partner
    def p_brand_get(conn, t, d):
        with conn.cursor() as cur:
            return {"branding": jsonable(partner.get_branding(cur, t)), "effective": jsonable(partner.effective_branding(cur, t))}

    def p_brand_save(conn, t, d):
        with conn.cursor() as cur:
            managers_only(cur, t, d, "change the branding")
            return {"branding": jsonable(partner.save_branding(cur, t, d))}

    def p_brand_effective(conn, t, d):
        with conn.cursor() as cur:
            return {"branding": jsonable(partner.effective_branding(cur, t))}

    def p_invite(conn, t, d):
        with conn.cursor() as cur:
            _owner_only(cur, t, d, "link this workspace to a reseller")
            return partner.invite(cur, t)

    def p_redeem(conn, t, d):
        need(d, "code")
        with conn.cursor() as cur:
            _owner_only(cur, t, d, "link a customer")
            return partner.redeem(cur, t, d["code"], d.get("commission_percent"), d.get("note") or "")

    def p_unlink(conn, t, d):
        with conn.cursor() as cur:
            _owner_only(cur, t, d, "end a link")
            return {"unlinked": partner.unlink(cur, t, d.get("child_tenant_id") or None)}

    def p_customers(conn, t, d):
        with conn.cursor() as cur:
            managers_only(cur, t, d, "see customer workspaces")
            return {"customers": jsonable(partner.customers(cur, t, days=int(d.get("days") or 30)))}

    def p_reseller(conn, t, d):
        with conn.cursor() as cur:
            return {"reseller": jsonable(partner.reseller_of(cur, t))}

    def p_percent(conn, t, d):
        need(d, "child_tenant_id", "commission_percent")
        with conn.cursor() as cur:
            _owner_only(cur, t, d, "change a commission")
            partner.set_percent(cur, t, d["child_tenant_id"], d["commission_percent"])
            return {"saved": True}

    def p_ledger_add(conn, t, d):
        need(d, "child_tenant_id", "period", "base")
        with conn.cursor() as cur:
            _owner_only(cur, t, d, "record commission")
            return jsonable(partner.ledger_add(cur, t, d["child_tenant_id"], d["period"], d["base"], d.get("note") or ""))

    def p_ledger_list(conn, t, d):
        with conn.cursor() as cur:
            managers_only(cur, t, d, "see commission")
            return jsonable(partner.ledger_list(cur, t, d.get("child_tenant_id") or None))

    def p_ledger_paid(conn, t, d):
        need(d, "entry_id")
        with conn.cursor() as cur:
            _owner_only(cur, t, d, "mark commission paid")
            return {"paid": partner.ledger_mark_paid(cur, t, d["entry_id"])}

    # coexistence
    def c_numbers(conn, t, d):
        with conn.cursor() as cur:
            return {"numbers": jsonable(coexistence.numbers(cur, t))}

    def c_sync(conn, t, d):
        need_vault()
        need(d, "number_id")
        with conn.cursor() as cur:
            managers_only(cur, t, d, "ask Meta for the phone's chats")
            return coexistence.request_sync(cur, t, graph, vault, d["number_id"])

    def c_mark(conn, t, d):
        need(d, "number_id")
        with conn.cursor() as cur:
            _owner_only(cur, t, d, "change how a number is connected")
            coexistence.mark(cur, t, d["number_id"], d.get("enabled") is not False)
            return {"numbers": jsonable(coexistence.numbers(cur, t))}

    return {"/internal/audit/list": a_list, "/internal/audit/export": a_export,
            "/internal/sso/config": s_config, "/internal/sso/save": s_save, "/internal/sso/test": s_test, "/internal/sso/start": s_start, "/internal/sso/complete": s_complete,
            "/internal/sso/status": s_status, "/internal/sso/required": s_required,
            "/internal/partner/branding/get": p_brand_get, "/internal/partner/branding/save": p_brand_save, "/internal/partner/branding/effective": p_brand_effective,
            "/internal/partner/invite": p_invite, "/internal/partner/redeem": p_redeem, "/internal/partner/unlink": p_unlink, "/internal/partner/customers": p_customers,
            "/internal/partner/reseller": p_reseller, "/internal/partner/set_percent": p_percent, "/internal/partner/ledger/add": p_ledger_add,
            "/internal/partner/ledger/list": p_ledger_list, "/internal/partner/ledger/paid": p_ledger_paid,
            "/internal/coexistence/numbers": c_numbers, "/internal/coexistence/sync": c_sync, "/internal/coexistence/mark": c_mark}
