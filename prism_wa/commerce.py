"""The shop side of a chat: the catalogue, the product messages, carts that arrive, and orders.

Money is whole paise everywhere (never a decimal), and an order's total always equals items − discount + tax + shipping to the paisa, because Meta refuses an
`order_details` message whose parts do not add up. A cart a customer builds in WhatsApp arrives as an inbound `order` message; it becomes a draft order that a person
(or a flow) turns into a payment request (payments.py).

  config      get / save   catalogue id, currency, tax, shipping, how customers pay (Meta's payments or Razorpay links) — gateway secrets are stored encrypted, shown never
  products    list / save / delete / import a CSV / pull from or push to the Meta catalogue
  messages    one product, a list of products, or the whole catalogue, sent to a chat (inside the 24-hour window)
  orders      create (from the catalogue or by hand) / get / list / change state / cancel; and `on_cart`, the inbound cart
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from psycopg2.extras import Json

from prism_wa import send, webhooks_out
from prism_wa.vault import Vault

STATES = ("draft", "awaiting_payment", "paid", "processing", "shipped", "completed", "cancelled", "failed", "refunded")
# where an order can go from where it is; a person moving it by hand follows the same rules the payment webhooks do
NEXT = {
    "draft": ("awaiting_payment", "cancelled"),
    "awaiting_payment": ("paid", "failed", "cancelled", "draft"),
    "failed": ("awaiting_payment", "cancelled", "draft"),
    "paid": ("processing", "shipped", "completed", "refunded", "cancelled"),
    "processing": ("shipped", "completed", "refunded", "cancelled"),
    "shipped": ("completed", "refunded"),
    "completed": ("refunded",),
    "cancelled": (),
    "refunded": (),
}
MAX_ITEMS = 60
MAX_LIST = 30
MAX_SECTIONS = 10
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class CommerceError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# ── money ────────────────────────────────────────────────────────────────────

def paise(value, *, what: str = "amount") -> int:
    """Rupees (a number or text like "499" / "1,299.50" / "Rs 499") -> whole paise. Refuses anything that is not a non-negative amount."""
    if isinstance(value, bool):
        raise CommerceError("bad_amount", "the %s is not a number" % what)
    if isinstance(value, int):
        d = Decimal(value)
    else:
        text = re.sub(r"(?i)rs\.?|inr|₹|,|\s", "", str(value if value is not None else "")).strip()
        try:
            d = Decimal(text)
        except InvalidOperation:
            raise CommerceError("bad_amount", "the %s is not a number" % what) from None
    if d < 0 or d > Decimal("100000000"):
        raise CommerceError("bad_amount", "the %s must be between 0 and 100,000,000" % what)
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rupees(amount_paise: int) -> str:
    return "{:,.2f}".format(Decimal(int(amount_paise or 0)) / 100)


def money_text(amount_paise: int, currency: str = "INR") -> str:
    return ("Rs " if currency == "INR" else currency + " ") + rupees(amount_paise)


def new_reference() -> str:
    """At most 35 characters of letters, digits, dash and underscore: what Meta and the gateway accept as a reference."""
    return "PR" + format(int(time.time()), "x").upper() + secrets.token_hex(3).upper()


def clean_reference(text: str) -> str:
    ref = re.sub(r"[^A-Za-z0-9_\-]", "", text or "")[:35]
    if not ref:
        raise CommerceError("bad_reference", "a reference is letters and numbers, up to 35 of them")
    return ref


# ── settings ─────────────────────────────────────────────────────────────────

CONFIG_COLS = ("catalog_id", "catalog_name", "currency", "tax_percent", "shipping_paise", "order_expiry_hours", "gateway", "payment_configuration", "razorpay_key_id",
               "razorpay_secret_enc", "razorpay_webhook_enc", "updated_at")


def get_config(cur, tenant_id) -> dict:
    """What is set, with the gateway's secrets reduced to "is one stored"."""
    cur.execute("select " + ", ".join(CONFIG_COLS) + " from commerce_config where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    c = dict(zip(CONFIG_COLS, row)) if row else {"catalog_id": None, "catalog_name": None, "currency": "INR", "tax_percent": Decimal(0), "shipping_paise": 0, "order_expiry_hours": 48,
                                                    "gateway": "none", "payment_configuration": None, "razorpay_key_id": None, "razorpay_secret_enc": None,
                                                    "razorpay_webhook_enc": None, "updated_at": None}
    c["tax_percent"] = float(c["tax_percent"] or 0)
    c["razorpay_secret_set"] = bool(c.pop("razorpay_secret_enc"))
    c["razorpay_webhook_set"] = bool(c.pop("razorpay_webhook_enc"))
    c["ready"] = ready_reason(c) is None
    return c


def ready_reason(config: dict) -> Optional[str]:
    """Why customers cannot be asked to pay yet (None when they can)."""
    if config["gateway"] == "none":
        return "Choose how customers pay first (Commerce → Settings)."
    if config["gateway"] == "meta" and not config.get("payment_configuration"):
        return "Add the payment configuration name from WhatsApp Manager first."
    if config["gateway"] == "razorpay" and not (config.get("razorpay_key_id") and config.get("razorpay_secret_set")):
        return "Add your Razorpay key id and secret first."
    return None


def save_config(cur, tenant_id, vault: Optional[Vault], d: dict) -> dict:
    current = get_config(cur, tenant_id)
    new = dict(current)
    if "catalog_id" in d:
        cid = str(d["catalog_id"] or "").strip()
        if cid and not re.match(r"^[0-9]{5,25}$", cid):
            raise CommerceError("bad_catalog", "a catalogue id is a number, shown in Commerce Manager")
        new["catalog_id"] = cid or None
    if "catalog_name" in d:
        new["catalog_name"] = (str(d["catalog_name"] or "").strip()[:100]) or None
    if "currency" in d:
        cur_ = str(d["currency"] or "INR").strip().upper()
        if not CURRENCY_RE.match(cur_):
            raise CommerceError("bad_currency", "a currency is three letters, like INR")
        new["currency"] = cur_
    if "tax_percent" in d:
        try:
            tax = Decimal(str(d["tax_percent"] or 0))
        except InvalidOperation:
            raise CommerceError("bad_tax", "tax is a percentage, like 18") from None
        if tax < 0 or tax > 100:
            raise CommerceError("bad_tax", "tax is a percentage between 0 and 100")
        new["tax_percent"] = float(tax)
    if "shipping" in d:
        new["shipping_paise"] = paise(d["shipping"] or 0, what="shipping charge")
    if "order_expiry_hours" in d:
        try:
            hours = int(d["order_expiry_hours"]) if d["order_expiry_hours"] not in (None, "") else 48
        except (TypeError, ValueError):
            raise CommerceError("bad_expiry", "the hours a payment request stays open is a whole number") from None
        if not 1 <= hours <= 720:
            raise CommerceError("bad_expiry", "a payment request can stay open from 1 hour to 30 days")
        new["order_expiry_hours"] = hours
    if "gateway" in d:
        if d["gateway"] not in ("none", "meta", "razorpay"):
            raise CommerceError("bad_gateway", "customers pay through Meta's payments or Razorpay links")
        new["gateway"] = d["gateway"]
    if "payment_configuration" in d:
        new["payment_configuration"] = (str(d["payment_configuration"] or "").strip()[:100]) or None
    key_id = new["razorpay_key_id"]
    if "razorpay_key_id" in d:
        key_id = (str(d["razorpay_key_id"] or "").strip()) or None
        if key_id and not re.match(r"^rzp_(test|live)_[A-Za-z0-9]{8,}$", key_id):
            raise CommerceError("bad_key", "a Razorpay key id looks like rzp_live_… or rzp_test_…")
        new["razorpay_key_id"] = key_id
    secrets_ = {}
    for field, col in (("razorpay_secret", "razorpay_secret_enc"), ("razorpay_webhook_secret", "razorpay_webhook_enc")):
        value = (d.get(field) or "").strip() if isinstance(d.get(field), str) else ""
        if value:
            if vault is None:
                raise LookupError("this server has no token key, so a gateway secret cannot be stored")
            secrets_[col] = vault.encrypt(value, context="commerce:%s:%s" % (tenant_id, col))
    cols = {"catalog_id": new["catalog_id"], "catalog_name": new["catalog_name"], "currency": new["currency"], "tax_percent": new["tax_percent"],
            "shipping_paise": new["shipping_paise"], "order_expiry_hours": new["order_expiry_hours"], "gateway": new["gateway"],
            "payment_configuration": new["payment_configuration"], "razorpay_key_id": key_id}
    sets = ", ".join("%s = excluded.%s" % (c, c) for c in cols)
    params = [tenant_id] + list(cols.values())
    extra_cols, extra_vals, extra_sets = "", [], ""
    ver = None
    for col, (blob, v) in secrets_.items():
        extra_cols += ", " + col
        extra_vals.append(blob)
        extra_sets += ", %s = excluded.%s" % (col, col)
        ver = v
    if ver is not None:
        extra_cols += ", key_ver"
        extra_vals.append(ver)
        extra_sets += ", key_ver = excluded.key_ver"
    cur.execute("insert into commerce_config (tenant_id, " + ", ".join(cols) + extra_cols + ") values (%s" + ", %s" * (len(cols) + len(extra_vals)) + ")"
                " on conflict (tenant_id) do update set " + sets + extra_sets + ", updated_at = now()", params + extra_vals)
    return get_config(cur, tenant_id)


def gateway_secrets(cur, tenant_id, vault: Vault) -> dict:
    """{key_id, secret, webhook_secret} for Razorpay; empty strings where nothing is stored."""
    cur.execute("select razorpay_key_id, razorpay_secret_enc, razorpay_webhook_enc from commerce_config where tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    if row is None:
        return {"key_id": "", "secret": "", "webhook_secret": ""}
    return {"key_id": row[0] or "",
            "secret": vault.decrypt(bytes(row[1]), context="commerce:%s:razorpay_secret_enc" % tenant_id) if row[1] else "",
            "webhook_secret": vault.decrypt(bytes(row[2]), context="commerce:%s:razorpay_webhook_enc" % tenant_id) if row[2] else ""}


# ── products ─────────────────────────────────────────────────────────────────

PRODUCT_COLS = ("id", "retailer_id", "name", "description", "price_paise", "sale_price_paise", "currency", "image_url", "url", "category", "available", "meta_product_id",
                "synced_at", "created_at", "updated_at")


def clean_product(d: dict, currency: str = "INR") -> dict:
    name = str(d.get("name") or "").strip()
    if not name:
        raise CommerceError("no_name", "a product needs a name")
    retailer = re.sub(r"[^A-Za-z0-9_\-.]", "", str(d.get("retailer_id") or "").strip())[:100]
    if not retailer:
        retailer = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or secrets.token_hex(4)
    if "price_paise" in d and d["price_paise"] is not None:
        price = int(d["price_paise"])
        if price < 0:
            raise CommerceError("bad_amount", "the price cannot be below zero")
    else:
        price = paise(d.get("price"), what="price")
    sale = None
    if d.get("sale_price_paise") not in (None, ""):
        sale = int(d["sale_price_paise"])
    elif d.get("sale_price") not in (None, ""):
        sale = paise(d["sale_price"], what="sale price")
    if sale is not None and sale > price:
        raise CommerceError("bad_amount", "the sale price cannot be more than the price")
    for key in ("image_url", "url"):
        v = str(d.get(key) or "").strip()
        if v and not re.match(r"^https?://", v, re.I):
            raise CommerceError("bad_link", "the %s must start with https://" % key.replace("_", " "))
    return {"retailer_id": retailer, "name": name[:150], "description": (str(d.get("description") or "").strip()[:5000]) or None,
            "price_paise": price, "sale_price_paise": sale, "currency": (d.get("currency") or currency or "INR").upper(),
            "image_url": (str(d.get("image_url") or "").strip()) or None, "url": (str(d.get("url") or "").strip()) or None,
            "category": (str(d.get("category") or "").strip()[:100]) or None, "available": d.get("available") is not False}


def save_product(cur, tenant_id, d: dict) -> dict:
    cfg = get_config(cur, tenant_id)
    p = clean_product(d, cfg["currency"])
    if d.get("id"):
        cur.execute("update products set retailer_id = %s, name = %s, description = %s, price_paise = %s, sale_price_paise = %s, currency = %s, image_url = %s, url = %s,"
                    " category = %s, available = %s, updated_at = now() where id = %s and tenant_id = %s returning id",
                    (p["retailer_id"], p["name"], p["description"], p["price_paise"], p["sale_price_paise"], p["currency"], p["image_url"], p["url"], p["category"], p["available"],
                     d["id"], tenant_id))
        row = cur.fetchone()
        if row is None:
            raise CommerceError("unknown_product", "that product is not in this workspace")
        return get_product(cur, tenant_id, row[0])
    cur.execute("insert into products (tenant_id, retailer_id, name, description, price_paise, sale_price_paise, currency, image_url, url, category, available)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (tenant_id, retailer_id) do update set name = excluded.name,"
                " description = excluded.description, price_paise = excluded.price_paise, sale_price_paise = excluded.sale_price_paise, currency = excluded.currency,"
                " image_url = excluded.image_url, url = excluded.url, category = excluded.category, available = excluded.available, updated_at = now() returning id",
                (tenant_id, p["retailer_id"], p["name"], p["description"], p["price_paise"], p["sale_price_paise"], p["currency"], p["image_url"], p["url"], p["category"],
                 p["available"]))
    return get_product(cur, tenant_id, cur.fetchone()[0])


def get_product(cur, tenant_id, product_id) -> dict:
    cur.execute("select " + ", ".join(PRODUCT_COLS) + " from products where id = %s and tenant_id = %s", (product_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise CommerceError("unknown_product", "that product is not in this workspace")
    return dict(zip(PRODUCT_COLS, row))


def list_products(cur, tenant_id, *, q: str = "", category: str = "", available: Optional[bool] = None, limit: int = 200, offset: int = 0) -> dict:
    where, params = ["tenant_id = %s"], [tenant_id]
    if q:
        where.append("(name ilike %s or retailer_id ilike %s or coalesce(category, '') ilike %s)")
        params += ["%" + q + "%"] * 3
    if category:
        where.append("category = %s")
        params.append(category)
    if available is not None:
        where.append("available = %s")
        params.append(available)
    cur.execute("select count(*) from products where " + " and ".join(where), params)
    total = cur.fetchone()[0]
    cur.execute("select " + ", ".join(PRODUCT_COLS) + " from products where " + " and ".join(where) + " order by name, retailer_id limit %s offset %s",
                params + [max(1, min(int(limit), 500)), max(0, int(offset))])
    rows = [dict(zip(PRODUCT_COLS, r)) for r in cur.fetchall()]
    cur.execute("select distinct category from products where tenant_id = %s and category is not null order by category", (tenant_id,))
    return {"products": rows, "total": total, "categories": [r[0] for r in cur.fetchall()]}


def delete_product(cur, tenant_id, product_id) -> bool:
    cur.execute("delete from products where id = %s and tenant_id = %s", (product_id, tenant_id))
    return cur.rowcount > 0


IMPORT_HEADERS = {"id": "retailer_id", "retailer_id": "retailer_id", "sku": "retailer_id", "title": "name", "name": "name", "description": "description", "price": "price",
                  "sale_price": "sale_price", "image_link": "image_url", "image_url": "image_url", "link": "url", "url": "url", "category": "category",
                  "product_type": "category", "availability": "available", "available": "available"}


def import_csv(cur, tenant_id, text: str) -> dict:
    """A catalogue CSV (Meta's own feed headers work: id, title, description, price, sale_price, image_link, link, availability). One bad row does not stop the rest."""
    reader = csv.DictReader(io.StringIO(text or ""))
    if not reader.fieldnames:
        raise CommerceError("bad_file", "the file has no header row")
    mapping = {h: IMPORT_HEADERS[h.strip().lower()] for h in reader.fieldnames if h and h.strip().lower() in IMPORT_HEADERS}
    if "name" not in mapping.values() or "price" not in mapping.values():
        raise CommerceError("bad_file", "the file needs at least a title (or name) column and a price column")
    added = updated = 0
    problems = []
    for n, row in enumerate(reader, start=2):
        d = {mapping[h]: v for h, v in row.items() if h in mapping and v is not None}
        availability = str(d.get("available", "in stock")).strip().lower()
        d["available"] = availability in ("in stock", "true", "yes", "1", "available", "")
        price_text = str(d.get("price") or "")
        d["price"] = re.sub(r"(?i)\s*[A-Z]{3}$", "", price_text).strip()
        try:
            p = clean_product(d)
            cur.execute("select 1 from products where tenant_id = %s and retailer_id = %s", (tenant_id, p["retailer_id"]))
            existed = cur.fetchone() is not None
            cur.execute("savepoint product_row")
            save_product(cur, tenant_id, p)
            cur.execute("release savepoint product_row")
            updated += 1 if existed else 0
            added += 0 if existed else 1
        except CommerceError as why:
            problems.append({"row": n, "error": str(why)})
    return {"added": added, "updated": updated, "problems": problems[:50], "skipped": len(problems)}


def _price_of(text) -> Optional[int]:
    """Meta shows a price as "₹499.00" or "499.00 INR"; -> paise, or None."""
    if text in (None, ""):
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(text))
    try:
        return paise(cleaned) if cleaned else None
    except CommerceError:
        return None


def _token_for(cur, tenant_id, vault: Vault, graph_account_id=None) -> tuple:
    """(token, waba_id (Meta's), phone_number_id (Meta's)) for the workspace's one WhatsApp number."""
    cur.execute("select w.token_enc, w.waba_id, p.phone_number_id from phone_numbers p join wabas w on w.id = p.waba_id where p.tenant_id = %s order by p.created_at limit 1",
                (tenant_id,))
    row = cur.fetchone()
    if row is None:
        raise CommerceError("no_number", "connect a WhatsApp number first")
    return vault.decrypt(row[0], context=row[1]), row[1], row[2]


def pull_catalog(cur, tenant_id, graph, vault: Vault) -> dict:
    """Read the Meta catalogue's products into ours (matched on retailer id). Our own products that Meta does not have are left alone."""
    cfg = get_config(cur, tenant_id)
    if not cfg["catalog_id"]:
        raise CommerceError("no_catalog", "add the catalogue id first")
    token, _waba, _pnid = _token_for(cur, tenant_id, vault)
    added = updated = 0
    after = ""
    for _page in range(50):
        reply = graph.catalog_products(token, cfg["catalog_id"], after=after)
        for item in reply.get("data") or []:
            if not isinstance(item, dict) or not item.get("retailer_id"):
                continue
            price = _price_of(item.get("price"))
            if price is None:
                continue
            sale = _price_of(item.get("sale_price"))
            cur.execute("select id from products where tenant_id = %s and retailer_id = %s", (tenant_id, item["retailer_id"]))
            had = cur.fetchone()
            cur.execute(
                "insert into products (tenant_id, retailer_id, name, description, price_paise, sale_price_paise, currency, image_url, url, category, available, meta_product_id, synced_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) on conflict (tenant_id, retailer_id) do update set name = excluded.name,"
                " description = excluded.description, price_paise = excluded.price_paise, sale_price_paise = excluded.sale_price_paise, image_url = excluded.image_url,"
                " url = excluded.url, category = excluded.category, available = excluded.available, meta_product_id = excluded.meta_product_id, synced_at = now(), updated_at = now()",
                (tenant_id, item["retailer_id"][:100], str(item.get("name") or item["retailer_id"])[:150], (item.get("description") or None), price,
                 sale if sale is not None and sale <= price else None, (item.get("currency") or cfg["currency"]).upper(), item.get("image_url"), item.get("url"),
                 item.get("category"), str(item.get("availability") or "in stock").lower() in ("in stock", "available for order"), item.get("id")))
            updated += 1 if had else 0
            added += 0 if had else 1
        after = (((reply.get("paging") or {}).get("cursors") or {}).get("after")) if (reply.get("paging") or {}).get("next") else ""
        if not after:
            break
    return {"added": added, "updated": updated}


def push_catalog(cur, tenant_id, graph, vault: Vault) -> dict:
    """Send our products to the Meta catalogue (created or updated by retailer id). Prices go as whole paise with the currency, which is what the batch API takes."""
    cfg = get_config(cur, tenant_id)
    if not cfg["catalog_id"]:
        raise CommerceError("no_catalog", "add the catalogue id first")
    token, _waba, _pnid = _token_for(cur, tenant_id, vault)
    cur.execute("select id, retailer_id, name, description, price_paise, sale_price_paise, currency, image_url, url, available from products where tenant_id = %s order by name limit 5000",
                (tenant_id,))
    rows = cur.fetchall()
    sent = 0
    for start in range(0, len(rows), 200):
        batch = []
        for pid, rid, name, desc, price, sale, cur_, image, url, available in rows[start:start + 200]:
            data = {"id": rid, "title": name, "description": desc or name, "availability": "in stock" if available else "out of stock", "condition": "new",
                    "price": price, "currency": cur_, "link": url or "https://wa.me/", "image_link": image or ""}
            if sale is not None:
                data["sale_price"] = sale
            batch.append({"method": "UPDATE", "data": {k: v for k, v in data.items() if v != ""}})
        graph.catalog_batch(token, cfg["catalog_id"], batch)
        sent += len(batch)
    cur.execute("update products set synced_at = now() where tenant_id = %s", (tenant_id,))
    return {"sent": sent}


# ── product messages ─────────────────────────────────────────────────────────

def _catalog_id(cur, tenant_id) -> str:
    cfg = get_config(cur, tenant_id)
    if not cfg["catalog_id"]:
        raise CommerceError("no_catalog", "add the catalogue id first (Commerce → Settings)")
    return cfg["catalog_id"]


def send_product(cur, tenant_id, *, contact_id, retailer_id: str, body: str = "", footer: str = "", idem_key: str, number_id=None, sender: str = "agent", now=None):
    cid = _catalog_id(cur, tenant_id)
    get_retailer(cur, tenant_id, retailer_id)
    interactive = {"type": "product", "body": {"text": (body or "Have a look at this.")[:1024]}, "action": {"catalog_id": cid, "product_retailer_id": retailer_id}}
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    return send.enqueue_interactive(cur, tenant_id, contact_id=contact_id, interactive=interactive, preview="[product] %s" % retailer_id, idem_key=idem_key,
                                    number_id=number_id, sender=sender, now=now)


def get_retailer(cur, tenant_id, retailer_id: str) -> dict:
    cur.execute("select id from products where tenant_id = %s and retailer_id = %s", (tenant_id, retailer_id))
    row = cur.fetchone()
    if row is None:
        raise CommerceError("unknown_product", "there is no product %r in this workspace" % retailer_id)
    return get_product(cur, tenant_id, row[0])


def send_products(cur, tenant_id, *, contact_id, sections: list, header: str = "Our products", body: str = "", footer: str = "", idem_key: str, number_id=None,
                  sender: str = "agent", now=None):
    """`sections` = [{"title": "Walls", "retailer_ids": ["a", "b"]}]: up to 10 sections and 30 products in all."""
    cid = _catalog_id(cur, tenant_id)
    if not sections or len(sections) > MAX_SECTIONS:
        raise CommerceError("bad_sections", "send between 1 and %d groups of products" % MAX_SECTIONS)
    total = sum(len(s.get("retailer_ids") or []) for s in sections)
    if total < 1 or total > MAX_LIST:
        raise CommerceError("bad_sections", "a product list holds 1 to %d products" % MAX_LIST)
    out = []
    for s in sections:
        ids = [str(i) for i in s.get("retailer_ids") or []]
        for i in ids:
            get_retailer(cur, tenant_id, i)
        out.append({"title": (str(s.get("title") or "Products"))[:24], "product_items": [{"product_retailer_id": i} for i in ids]})
    interactive = {"type": "product_list", "header": {"type": "text", "text": header[:60]}, "body": {"text": (body or "Pick what you like.")[:1024]},
                   "action": {"catalog_id": cid, "sections": out}}
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    return send.enqueue_interactive(cur, tenant_id, contact_id=contact_id, interactive=interactive, preview="[%d products]" % total, idem_key=idem_key, number_id=number_id,
                                    sender=sender, now=now)


def send_catalog(cur, tenant_id, *, contact_id, body: str = "", footer: str = "", thumbnail_retailer_id: str = "", idem_key: str, number_id=None, sender: str = "agent", now=None):
    _catalog_id(cur, tenant_id)
    params = {"thumbnail_product_retailer_id": thumbnail_retailer_id} if thumbnail_retailer_id else {}
    interactive = {"type": "catalog_message", "body": {"text": (body or "Browse our catalogue.")[:1024]}, "action": {"name": "catalog_message", "parameters": params}}
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    return send.enqueue_interactive(cur, tenant_id, contact_id=contact_id, interactive=interactive, preview="[catalogue]", idem_key=idem_key, number_id=number_id, sender=sender, now=now)


# ── orders ───────────────────────────────────────────────────────────────────

ORDER_COLS = ("id", "contact_id", "conversation_id", "number_id", "reference_id", "source", "status", "items", "currency", "subtotal_paise", "tax_paise", "shipping_paise",
              "discount_paise", "total_paise", "address", "note", "expires_at", "created_at", "updated_at")


def totals(items: list, *, tax_percent=0, shipping_paise: int = 0, discount_paise: int = 0) -> dict:
    """The five numbers of an order. Tax is on the amount after discount, rounded to the paisa; the total is exactly their sum."""
    subtotal = sum(int(i["line_paise"]) for i in items)
    discount = max(0, min(int(discount_paise or 0), subtotal))
    tax = int((Decimal(subtotal - discount) * Decimal(str(tax_percent or 0)) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    shipping = max(0, int(shipping_paise or 0))
    return {"subtotal_paise": subtotal, "discount_paise": discount, "tax_paise": tax, "shipping_paise": shipping, "total_paise": subtotal - discount + tax + shipping}


def build_items(cur, tenant_id, raw: list) -> list:
    """Items from the catalogue ({retailer_id, quantity}) or by hand ({name, quantity, price}), each with its line total."""
    if not isinstance(raw, list) or not raw:
        raise CommerceError("no_items", "an order needs at least one item")
    if len(raw) > MAX_ITEMS:
        raise CommerceError("too_many_items", "an order holds up to %d different items" % MAX_ITEMS)
    items = []
    for r in raw:
        if not isinstance(r, dict):
            raise CommerceError("bad_item", "each item is a product and a quantity")
        try:
            qty = int(r["quantity"]) if r.get("quantity") not in (None, "") else 1
        except (TypeError, ValueError):
            raise CommerceError("bad_quantity", "the quantity is a whole number") from None
        if qty < 1 or qty > 1000:
            raise CommerceError("bad_quantity", "the quantity is between 1 and 1000")
        if r.get("retailer_id") and not r.get("name") and r.get("unit_paise") is None and r.get("price") is None:
            p = get_retailer(cur, tenant_id, str(r["retailer_id"]))
            unit = p["sale_price_paise"] if p["sale_price_paise"] is not None else p["price_paise"]
            name = p["name"]
        else:
            name = str(r.get("name") or r.get("retailer_id") or "").strip()
            if not name:
                raise CommerceError("bad_item", "each item needs a name")
            unit = int(r["unit_paise"]) if r.get("unit_paise") is not None else paise(r.get("price"), what="price")
        items.append({"retailer_id": str(r.get("retailer_id") or "")[:100], "name": name[:150], "quantity": qty, "unit_paise": unit, "line_paise": unit * qty})
    return items


def get_order(cur, tenant_id, order_id) -> dict:
    cur.execute("select " + ", ".join("o." + c for c in ORDER_COLS) + ", c.name, c.phone from orders o left join contacts c on c.id = o.contact_id"
                " where o.id = %s and o.tenant_id = %s", (order_id, tenant_id))
    row = cur.fetchone()
    if row is None:
        raise CommerceError("unknown_order", "that order is not in this workspace")
    o = dict(zip(ORDER_COLS + ("contact_name", "contact_phone"), row))
    cur.execute("select id, provider, status, amount_paise, refunded_paise, method, link_url, created_at, paid_at, last_error from payments where order_id = %s order by created_at desc", (order_id,))
    o["payments"] = [dict(zip(("id", "provider", "status", "amount_paise", "refunded_paise", "method", "link_url", "created_at", "paid_at", "last_error"), r)) for r in cur.fetchall()]
    return o


def list_orders(cur, tenant_id, *, status: str = "", q: str = "", contact_id=None, limit: int = 100, offset: int = 0) -> dict:
    where, params = ["o.tenant_id = %s"], [tenant_id]
    if status:
        where.append("o.status = %s")
        params.append(status)
    if contact_id:
        where.append("o.contact_id = %s")
        params.append(contact_id)
    if q:
        where.append("(o.reference_id ilike %s or c.name ilike %s or c.phone ilike %s)")
        params += ["%" + q + "%"] * 3
    base = " from orders o left join contacts c on c.id = o.contact_id where " + " and ".join(where)
    cur.execute("select count(*)" + base, params)
    total = cur.fetchone()[0]
    cur.execute("select o.status, count(*)" + base + " group by o.status", params)
    counts = dict(cur.fetchall())
    cur.execute("select " + ", ".join("o." + c for c in ORDER_COLS) + ", c.name, c.phone" + base + " order by o.created_at desc limit %s offset %s",
                params + [max(1, min(int(limit), 500)), max(0, int(offset))])
    cols = ORDER_COLS + ("contact_name", "contact_phone")
    return {"orders": [dict(zip(cols, r)) for r in cur.fetchall()], "total": total, "counts": counts}


def create_order(cur, tenant_id, d: dict, *, agent_id=None, source: str = "manual") -> dict:
    cfg = get_config(cur, tenant_id)
    items = build_items(cur, tenant_id, d.get("items"))
    sums = totals(items, tax_percent=d.get("tax_percent", cfg["tax_percent"]),
                  shipping_paise=paise(d["shipping"]) if d.get("shipping") not in (None, "") else cfg["shipping_paise"],
                  discount_paise=paise(d["discount"]) if d.get("discount") not in (None, "") else 0)
    contact_id = d.get("contact_id")
    if contact_id:
        cur.execute("select 1 from contacts where id = %s and tenant_id = %s", (contact_id, tenant_id))
        if cur.fetchone() is None:
            raise CommerceError("unknown_contact", "that contact is not in this workspace")
    ref = clean_reference(d["reference_id"]) if d.get("reference_id") else new_reference()
    cur.execute("select id, number_id from conversations where tenant_id = %s and contact_id = %s and channel = 'whatsapp'", (tenant_id, contact_id)) if contact_id else None
    conv = cur.fetchone() if contact_id else None
    cur.execute("insert into orders (tenant_id, contact_id, conversation_id, number_id, reference_id, source, status, items, currency, subtotal_paise, tax_paise, shipping_paise,"
                " discount_paise, total_paise, address, note, created_by) values (%s, %s, %s, %s, %s, %s, 'draft', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
                (tenant_id, contact_id, conv[0] if conv else None, conv[1] if conv else None, ref, source, Json(items), cfg["currency"], sums["subtotal_paise"],
                 sums["tax_paise"], sums["shipping_paise"], sums["discount_paise"], sums["total_paise"], Json(d["address"]) if isinstance(d.get("address"), dict) else None,
                 (str(d.get("note") or "").strip()[:1000]) or None, agent_id))
    return get_order(cur, tenant_id, cur.fetchone()[0])


def update_order(cur, tenant_id, order_id, d: dict) -> dict:
    """Change the items, charges, address or note of an order that has not been paid for yet."""
    o = get_order(cur, tenant_id, order_id)
    if o["status"] not in ("draft", "failed"):
        raise CommerceError("order_locked", "an order cannot be changed once a payment was asked for; cancel it and make a new one")
    cfg = get_config(cur, tenant_id)
    items = build_items(cur, tenant_id, d["items"]) if "items" in d else o["items"]
    sums = totals(items, tax_percent=d.get("tax_percent", cfg["tax_percent"]),
                  shipping_paise=paise(d["shipping"]) if d.get("shipping") not in (None, "") else o["shipping_paise"],
                  discount_paise=paise(d["discount"]) if d.get("discount") not in (None, "") else o["discount_paise"])
    cur.execute("update orders set items = %s, subtotal_paise = %s, tax_paise = %s, shipping_paise = %s, discount_paise = %s, total_paise = %s,"
                " address = coalesce(%s, address), note = coalesce(%s, note), updated_at = now() where id = %s",
                (Json(items), sums["subtotal_paise"], sums["tax_paise"], sums["shipping_paise"], sums["discount_paise"], sums["total_paise"],
                 Json(d["address"]) if isinstance(d.get("address"), dict) else None, d.get("note"), order_id))
    return get_order(cur, tenant_id, order_id)


def set_status(cur, tenant_id, order_id, status: str, *, note: str = "", notify: bool = True, sender: str = "system") -> dict:
    """Move an order along. A move that skips the rules is refused. The customer is told in the chat (when there is a chat and its window is open)."""
    if status not in STATES:
        raise CommerceError("bad_status", "an order is one of: %s" % ", ".join(STATES))
    o = get_order(cur, tenant_id, order_id)
    if status == o["status"]:
        return o
    if status not in NEXT[o["status"]]:
        raise CommerceError("bad_move", "an order that is %s cannot become %s" % (o["status"].replace("_", " "), status.replace("_", " ")))
    cur.execute("update orders set status = %s, note = coalesce(nullif(%s, ''), note), updated_at = now() where id = %s", (status, note, order_id))
    if notify and o["contact_id"] and status in ("processing", "shipped", "completed", "cancelled"):
        from prism_wa import payments            # (payments imports this module for money helpers)
        payments.tell_customer_status(cur, tenant_id, get_order(cur, tenant_id, order_id), status, note, sender=sender)
    return get_order(cur, tenant_id, order_id)


def cancel(cur, tenant_id, order_id, *, vault=None, http=None) -> dict:
    o = get_order(cur, tenant_id, order_id)
    for pay in o["payments"]:
        if pay["status"] in ("created", "pending"):
            from prism_wa import payments
            payments.close_open(cur, tenant_id, pay["id"], vault, http, "cancelled")
    return set_status(cur, tenant_id, order_id, "cancelled", notify=True)


# ── a cart that arrived ──────────────────────────────────────────────────────

def on_cart(cur, tenant_id, *, contact_id, conversation_id, number_id, event_raw: dict, wamid: str, canonical: dict) -> Optional[str]:
    """An inbound `order` message (the customer's cart) -> a draft order. The same message delivered twice makes one order."""
    message = (event_raw.get("message") if isinstance(event_raw, dict) else None) or {}
    body = message.get("order") if isinstance(message.get("order"), dict) else {}
    raw_items = [i for i in (body.get("product_items") or []) if isinstance(i, dict) and i.get("product_retailer_id")]
    if not raw_items:
        return None
    cfg = get_config(cur, tenant_id)
    items = []
    for i in raw_items[:MAX_ITEMS]:
        rid = str(i["product_retailer_id"])[:100]
        qty = max(1, min(int(i.get("quantity") or 1), 1000))
        cur.execute("select name, price_paise, sale_price_paise from products where tenant_id = %s and retailer_id = %s", (tenant_id, rid))
        known = cur.fetchone()
        unit = _item_price(i.get("item_price"), i.get("currency") or cfg["currency"])
        if unit is None:
            unit = (known[2] if known and known[2] is not None else known[1]) if known else 0
        items.append({"retailer_id": rid, "name": known[0] if known else rid, "quantity": qty, "unit_paise": unit, "line_paise": unit * qty})
    sums = totals(items, tax_percent=cfg["tax_percent"], shipping_paise=cfg["shipping_paise"])
    ref = "WA" + hashlib.sha256((wamid or str(body)).encode("utf-8")).hexdigest()[:16].upper()
    cur.execute("insert into orders (tenant_id, contact_id, conversation_id, number_id, reference_id, source, status, items, currency, subtotal_paise, tax_paise, shipping_paise,"
                " discount_paise, total_paise, note) values (%s, %s, %s, %s, %s, 'cart', 'draft', %s, %s, %s, %s, %s, 0, %s, %s)"
                " on conflict (tenant_id, reference_id) do nothing returning id",
                (tenant_id, contact_id, conversation_id, number_id, ref, Json(items), cfg["currency"], sums["subtotal_paise"], sums["tax_paise"], sums["shipping_paise"],
                 sums["total_paise"], (str(body.get("text") or "").strip()[:1000]) or None))
    row = cur.fetchone()
    if row is None:
        return None
    webhooks_out.emit(cur, tenant_id, "order.received", lambda: {"order_id": str(row[0]), "reference_id": ref, "contact_id": str(contact_id), "total_paise": sums["total_paise"],
                                                                 "items": items})
    cur.execute("update contacts set tags = array(select distinct t from unnest(coalesce(tags, '{}') || %s::text[]) t order by t) where id = %s", (["cart"], contact_id))
    return str(row[0])


def _item_price(value, currency: str) -> Optional[int]:
    """A cart item's price comes as a number in rupees (WhatsApp sends the catalogue price); -> paise."""
    if value in (None, ""):
        return None
    try:
        return paise(value, what="price")
    except CommerceError:
        return None
