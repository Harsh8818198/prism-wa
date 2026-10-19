"""Routes for the Templates tab beyond create / sync / delete.

    templates   library (by industry)   upload_sample (a header picture / video / document for Meta's review)
                generate  rewrite       (the language model writes, we check)
                stats                   (what each template has done: sent, delivered, read, failed, clicked)
"""
from __future__ import annotations

import uuid

from prism_wa import ai_agent, media, template_ai, template_library, templates
from prism_wa.ops_common import jsonable, need

SAMPLE_KINDS = {"image": "image", "video": "video", "document": "document"}


def templates_library(cur, t, d):
    return {"library": template_library.LIBRARY, "industries": template_library.INDUSTRIES}


def templates_generate(cur, t, d):
    need(d, "description")
    return template_ai.generate(ai_agent.LLM_CLIENT, description=d["description"], category=d.get("category") or "", tone=d.get("tone") or "friendly",
                                goal=d.get("goal") or "replies", language=d.get("language") or "en_US", count=d.get("count") or 3,
                                industry=d.get("industry") or "")


def templates_rewrite(cur, t, d):
    need(d, "template_id")
    try:
        key = str(uuid.UUID(str(d["template_id"])))
    except ValueError:
        raise ValueError("that is not a template id") from None
    cur.execute("select name, language, category, status, reject_reason, components from templates where id = %s and tenant_id = %s", (key, t))
    row = cur.fetchone()
    if row is None:
        raise LookupError("that template is not in this workspace")
    template = dict(zip(("name", "language", "category", "status", "reject_reason", "components"), row))
    return template_ai.rewrite(ai_agent.LLM_CLIENT, template)


def templates_stats(cur, t, d):
    """Per template (name + language): messages sent, delivered, read, failed; and the clicks its campaigns got."""
    cur.execute(
        "select o.payload->'template'->>'name', o.payload->'template'->'language'->>'code',"
        "       count(*) filter (where o.state = 'sent'),"
        "       count(*) filter (where m.status in ('delivered', 'read')),"
        "       count(*) filter (where m.status = 'read'),"
        "       count(*) filter (where o.state = 'failed' or m.status = 'failed'),"
        "       max(o.created_at)"
        " from outbox o left join messages m on m.id = o.message_id"
        " where o.tenant_id = %s and o.kind = 'template' group by 1, 2", (t,))
    stats = {(r[0], r[1]): {"name": r[0], "language": r[1], "sent": r[2], "delivered": r[3], "read": r[4], "failed": r[5], "last_sent": r[6],
                            "clicks": 0} for r in cur.fetchall()}
    cur.execute("select c.template_name, c.language, count(*) from click_events e join campaigns c on c.id = e.campaign_id"
                " where c.tenant_id = %s group by 1, 2", (t,))
    for name, lang, n in cur.fetchall():
        key = (name, lang or "en_US")
        if key in stats:
            stats[key]["clicks"] = n
    rows = sorted(stats.values(), key=lambda r: (r["name"] or "", r["language"] or ""))
    for r in rows:
        r["read_rate"] = round(100.0 * r["read"] / r["sent"], 1) if r["sent"] else 0.0
    return jsonable({"stats": rows})


def make_conn_ops(graph, vault, app_id: str) -> dict:
    def t_upload_sample(conn, t, d):
        """Give Meta a picture / video / document to look at when it reviews a template with a media header."""
        need(d, "asset_id")
        if vault is None:
            raise LookupError("template submission is not set up on this server (no token key)")
        if not app_id:
            raise LookupError("uploading a sample needs this service to know the Meta app (WA_APP_ID)")
        want = SAMPLE_KINDS.get(str(d.get("format") or "").lower())
        with conn.cursor() as cur:
            asset, data = media.read(cur, t, d["asset_id"])
            if want and asset["kind"] != want:
                raise ValueError("that file is a %s, but this template's header is a %s" % (asset["kind"], want))
            if asset["kind"] not in SAMPLE_KINDS.values():
                raise ValueError("a template header can show a picture, a video or a document")
            cur.execute("select id from wabas where tenant_id = %s order by created_at limit 2", (t,))
            wabas = cur.fetchall()
            if d.get("waba_id"):
                wabas = [(d["waba_id"],)]
            if len(wabas) != 1:
                raise LookupError("this workspace has no WhatsApp Business Account yet" if not wabas else "say which WhatsApp Business Account")
            _meta, token = templates._waba_token(cur, vault, t, wabas[0][0])
        handle = graph.resumable_upload(token, app_id, asset["filename"], asset["mime"], data)
        return {"handle": handle, "asset_id": str(asset["id"]), "kind": asset["kind"]}
    return {"/internal/templates/upload_sample": t_upload_sample}


OPS = {"/internal/templates/library": templates_library, "/internal/templates/generate": templates_generate,
       "/internal/templates/rewrite": templates_rewrite, "/internal/templates/stats": templates_stats}
