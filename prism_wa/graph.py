"""A small client for the parts of Meta's Graph API the platform uses.

The transport is injectable (`transport(method, url, body, headers) -> (status,
raw_bytes)`), so every caller is tested against a fake Graph server that can
return any error code, and nothing in the test suite touches Meta's network.

The version defaults to v25.0 (the Marketing API version in the spec, §1.2) and
is overridable with WA_GRAPH_VERSION; check it against the WhatsApp changelog
before going live.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable, Optional

BASE = "https://graph.facebook.com"
DEFAULT_VERSION = "v25.0"


class GraphError(Exception):
    """Meta (or the network) said no. `code` is Meta's error code, None for a
    network failure; `http_status` is 0 when there was no response at all."""

    def __init__(self, message: str, *, code: Optional[int] = None, subcode: Optional[int] = None,
                 http_status: int = 0, fbtrace_id: str = "", details: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.http_status = http_status
        self.fbtrace_id = fbtrace_id
        self.details = details

    def as_dict(self) -> dict:
        return {"message": self.message, "code": self.code, "subcode": self.subcode,
                "http_status": self.http_status, "fbtrace_id": self.fbtrace_id, "details": self.details}


def urllib_transport(method: str, url: str, body: Optional[bytes], headers: dict, timeout: float = 30.0):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:         # noqa: S310 (fixed https host)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GraphError("network error: %s" % exc) from exc


class Graph:
    def __init__(self, transport: Optional[Callable] = None, *, version: Optional[str] = None,
                 base: str = BASE):
        self.transport = transport or urllib_transport
        self.version = version or os.environ.get("WA_GRAPH_VERSION") or DEFAULT_VERSION
        self.base = base.rstrip("/")

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _url(self, path: str, query: Optional[dict] = None) -> str:
        url = "%s/%s/%s" % (self.base, self.version, path.lstrip("/"))
        return url + ("?" + urllib.parse.urlencode(query) if query else "")

    def _call(self, method: str, path: str, *, token: Optional[str] = None, body: Optional[dict] = None,
              query: Optional[dict] = None) -> dict:
        headers = {"Accept": "application/json"}
        data = None
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = self.transport(method, self._url(path, query), data, headers)
        return self._parsed(status, raw)

    @staticmethod
    def _parsed(status: int, raw: bytes) -> dict:
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            parsed = {}
        if status >= 400 or (isinstance(parsed, dict) and "error" in parsed):
            err = parsed.get("error") if isinstance(parsed, dict) else None
            err = err if isinstance(err, dict) else {}
            data_ = err.get("error_data") if isinstance(err.get("error_data"), dict) else {}
            raise GraphError(str(err.get("message") or "HTTP %d" % status),
                             code=err.get("code"), subcode=err.get("error_subcode"), http_status=status,
                             fbtrace_id=str(err.get("fbtrace_id") or ""),
                             details=str(data_.get("details") or err.get("error_user_msg") or ""))
        return parsed if isinstance(parsed, dict) else {}

    def _multipart(self, path: str, token: str, fields: dict, file_field: str, filename: str, mime: str, data: bytes) -> dict:
        crlf = "\r\n"
        boundary = "----prismwa" + uuid.uuid4().hex
        parts = []
        for name, value in fields.items():
            parts.append(("--%s%sContent-Disposition: form-data; name=\"%s\"%s%s%s%s" % (boundary, crlf, name, crlf, crlf, value, crlf)).encode("utf-8"))
        safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
        head = "--%s%sContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"%sContent-Type: %s%s%s" % (
            boundary, crlf, file_field, safe, crlf, mime, crlf, crlf)
        parts.append(head.encode("utf-8"))
        parts.append(data)
        parts.append(("%s--%s--%s" % (crlf, boundary, crlf)).encode("utf-8"))
        headers = {"Accept": "application/json", "Authorization": "Bearer " + token, "Content-Type": "multipart/form-data; boundary=" + boundary}
        status, raw = self.transport("POST", self._url(path), b"".join(parts), headers)
        return self._parsed(status, raw)

    # ── what the platform needs ──────────────────────────────────────────────
    def send_message(self, token: str, phone_number_id: str, body: dict) -> dict:
        return self._call("POST", "%s/messages" % phone_number_id, token=token, body=body)

    # ── media ────────────────────────────────────────────────────────────────
    def upload_media(self, token: str, phone_number_id: str, filename: str, mime: str, data: bytes) -> str:
        """Give Meta a file to send; -> its media id (valid for about 30 days, usable by this number)."""
        out = self._multipart("%s/media" % phone_number_id, token, {"messaging_product": "whatsapp"}, "file", filename, mime, data)
        if not out.get("id"):
            raise GraphError("Meta did not return a media id")
        return str(out["id"])

    def media_info(self, token: str, media_id: str) -> dict:
        """-> {id, url, mime_type, file_size, sha256}. The url expires in minutes: fetch the file at once."""
        return self._call("GET", str(media_id), token=token)

    def download_media(self, token: str, url: str) -> bytes:
        status, raw = self.transport("GET", url, None, {"Authorization": "Bearer " + token})
        if status >= 400:
            raise GraphError("Meta would not give the file (HTTP %d)" % status, http_status=status)
        return raw

    def resumable_upload(self, token: str, app_id: str, filename: str, mime: str, data: bytes) -> str:
        """A file handle for a template's example header (image / video / document): open an upload session, send the bytes."""
        session = self._call("POST", "%s/uploads" % app_id, token=token, query={"file_name": filename, "file_length": len(data), "file_type": mime})
        sid = session.get("id")
        if not sid:
            raise GraphError("Meta did not open an upload session")
        headers = {"Accept": "application/json", "Authorization": "OAuth " + token, "file_offset": "0", "Content-Type": mime}
        status, raw = self.transport("POST", self._url(str(sid)), data, headers)
        out = self._parsed(status, raw)
        if not out.get("h"):
            raise GraphError("Meta did not return a file handle")
        return str(out["h"])

    def send_conversion_event(self, token: str, dataset_id: str, body: dict) -> dict:
        """The Conversions API: tell Meta what a click-to-WhatsApp ad earned (a lead, a purchase)."""
        return self._call("POST", "%s/events" % dataset_id, token=token, body=body)

    # ── the number itself and the profile customers see ──────────────────────
    def phone_number_info(self, token: str, phone_number_id: str) -> dict:
        """The display name and its review, quality, the daily limit on starting chats (messaging_limit_tier) and speed."""
        return self._call("GET", str(phone_number_id), token=token,
                          query={"fields": "verified_name,name_status,quality_rating,messaging_limit_tier,throughput,display_phone_number,status"})

    def business_profile(self, token: str, phone_number_id: str) -> dict:
        out = self._call("GET", "%s/whatsapp_business_profile" % phone_number_id, token=token,
                         query={"fields": "about,address,description,email,profile_picture_url,websites,vertical"})
        rows = out.get("data") or []
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    def update_business_profile(self, token: str, phone_number_id: str, fields: dict) -> dict:
        return self._call("POST", "%s/whatsapp_business_profile" % phone_number_id, token=token, body={"messaging_product": "whatsapp", **fields})

    def exchange_code(self, app_id: str, app_secret: str, code: str) -> str:
        """Embedded Signup: trade the one-time code for the customer's business token."""
        out = self._call("GET", "oauth/access_token",
                         query={"client_id": app_id, "client_secret": app_secret, "code": code})
        token = out.get("access_token")
        if not token:
            raise GraphError("Meta did not return an access token")
        return str(token)

    def register_number(self, token: str, phone_number_id: str, pin: str) -> dict:
        return self._call("POST", "%s/register" % phone_number_id, token=token,
                          body={"messaging_product": "whatsapp", "pin": pin})

    # ── templates ────────────────────────────────────────────────────────────
    TEMPLATE_FIELDS = "id,name,language,category,status,components,quality_score,rejected_reason"

    def list_templates(self, token: str, waba_id: str, *, after: str = "", limit: int = 100) -> dict:
        """One page of a WABA's templates. The reply carries paging.cursors.after when there is more."""
        query = {"fields": self.TEMPLATE_FIELDS, "limit": limit}
        if after:
            query["after"] = after
        return self._call("GET", "%s/message_templates" % waba_id, token=token, query=query)

    def create_template(self, token: str, waba_id: str, body: dict) -> dict:
        return self._call("POST", "%s/message_templates" % waba_id, token=token, body=body)

    def delete_template(self, token: str, waba_id: str, name: str, hsm_id: str = "") -> dict:
        """Delete a template. By name alone Meta removes EVERY language of it; with `hsm_id` only that one."""
        query = {"name": name}
        if hsm_id:
            query["hsm_id"] = hsm_id
        return self._call("DELETE", "%s/message_templates" % waba_id, token=token, query=query)

    def subscribe_app(self, token: str, waba_id: str, *, override_callback_uri: str = "",
                      verify_token: str = "") -> dict:
        """Subscribe our app to a customer's WABA so its webhooks reach us. With
        `override_callback_uri` that WABA's events go to its own URL."""
        body = {}
        if override_callback_uri:
            body = {"override_callback_uri": override_callback_uri, "verify_token": verify_token}
        return self._call("POST", "%s/subscribed_apps" % waba_id, token=token, body=body or None)

    # ── catalogue and commerce ───────────────────────────────────────────────
    PRODUCT_FIELDS = "retailer_id,name,description,price,sale_price,currency,image_url,url,availability,category"

    def waba_catalogs(self, token: str, waba_id: str) -> list:
        """The catalogues connected to a WABA: [{id, name}]."""
        return list(self._call("GET", "%s/product_catalogs" % waba_id, token=token, query={"fields": "id,name"}).get("data") or [])

    def connect_catalog(self, token: str, waba_id: str, catalog_id: str) -> dict:
        return self._call("POST", "%s/product_catalogs" % waba_id, token=token, body={"catalog_id": catalog_id})

    def commerce_settings(self, token: str, phone_number_id: str) -> dict:
        out = self._call("GET", "%s/whatsapp_commerce_settings" % phone_number_id, token=token)
        rows = out.get("data") or []
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    def set_commerce_settings(self, token: str, phone_number_id: str, *, catalog_visible: Optional[bool] = None, cart_enabled: Optional[bool] = None) -> dict:
        query = {}
        if catalog_visible is not None:
            query["is_catalog_visible"] = "true" if catalog_visible else "false"
        if cart_enabled is not None:
            query["is_cart_enabled"] = "true" if cart_enabled else "false"
        return self._call("POST", "%s/whatsapp_commerce_settings" % phone_number_id, token=token, query=query)

    def catalog_products(self, token: str, catalog_id: str, *, after: str = "", limit: int = 100) -> dict:
        query = {"fields": self.PRODUCT_FIELDS, "limit": limit}
        if after:
            query["after"] = after
        return self._call("GET", "%s/products" % catalog_id, token=token, query=query)

    def catalog_batch(self, token: str, catalog_id: str, requests: list) -> dict:
        """Create / update / delete products in bulk: [{"method": "CREATE"|"UPDATE"|"DELETE", "data": {...}}]."""
        return self._call("POST", "%s/items_batch" % catalog_id, token=token, body={"item_type": "PRODUCT_ITEM", "requests": requests})

    # ── payments ─────────────────────────────────────────────────────────────
    def payment_configurations(self, token: str, waba_id: str) -> list:
        return list(self._call("GET", "%s/payment_configurations" % waba_id, token=token).get("data") or [])

    def payment_status(self, token: str, phone_number_id: str, reference_id: str) -> dict:
        """Meta's own record of a payment, by the reference id we put on the order."""
        path = os.environ.get("WA_PAYMENT_LOOKUP_PATH") or "{phone_number_id}/payments/{reference_id}"
        return self._call("GET", path.format(phone_number_id=phone_number_id, reference_id=urllib.parse.quote(reference_id, safe="")), token=token)

    # ── forms (WhatsApp Flows) ───────────────────────────────────────────────
    FLOW_FIELDS = "id,name,categories,status,validation_errors,json_version,data_api_version,endpoint_uri"

    def create_flow(self, token: str, waba_id: str, *, name: str, categories: list, endpoint_uri: str = "") -> dict:
        body = {"name": name, "categories": categories}
        if endpoint_uri:
            body["endpoint_uri"] = endpoint_uri
        return self._call("POST", "%s/flows" % waba_id, token=token, body=body)

    def update_flow(self, token: str, flow_id: str, *, name: str = "", categories: Optional[list] = None, endpoint_uri: str = "") -> dict:
        body = {k: v for k, v in (("name", name), ("categories", categories), ("endpoint_uri", endpoint_uri)) if v}
        return self._call("POST", str(flow_id), token=token, body=body)

    def upload_flow_json(self, token: str, flow_id: str, flow_json: dict) -> dict:
        """Give Meta a flow's JSON. Meta answers with `validation_errors` (a list; empty when it is fine)."""
        data = json.dumps(flow_json, separators=(",", ":")).encode("utf-8")
        return self._multipart("%s/assets" % flow_id, token, {"name": "flow.json", "asset_type": "FLOW_JSON"}, "file", "flow.json", "application/json", data)

    def publish_flow(self, token: str, flow_id: str) -> dict:
        return self._call("POST", "%s/publish" % flow_id, token=token)

    def deprecate_flow(self, token: str, flow_id: str) -> dict:
        return self._call("POST", "%s/deprecate" % flow_id, token=token)

    def delete_flow(self, token: str, flow_id: str) -> dict:
        return self._call("DELETE", str(flow_id), token=token)

    def get_flow(self, token: str, flow_id: str) -> dict:
        return self._call("GET", str(flow_id), token=token, query={"fields": self.FLOW_FIELDS + ",preview.invalidate(false)"})

    def list_flows(self, token: str, waba_id: str) -> list:
        return list(self._call("GET", "%s/flows" % waba_id, token=token, query={"fields": self.FLOW_FIELDS}).get("data") or [])

    def set_flow_encryption_key(self, token: str, phone_number_id: str, public_pem: str) -> dict:
        """Tell Meta the public half of the key it should encrypt a form's live requests with."""
        return self._call("POST", "%s/whatsapp_business_encryption" % phone_number_id, token=token, query={"business_public_key": public_pem})

    def flow_encryption_status(self, token: str, phone_number_id: str) -> dict:
        out = self._call("GET", "%s/whatsapp_business_encryption" % phone_number_id, token=token)
        rows = out.get("data") or []
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    # ── coexistence (a number also used in the WhatsApp Business app) ────────
    def request_app_sync(self, token: str, phone_number_id: str, sync_type: str) -> dict:
        """After onboarding, ask Meta to send the phone app's contacts ("smb_app_state_sync") or the last six months of chats ("history")."""
        return self._call("POST", "%s/smb_app_data" % phone_number_id, token=token, body={"messaging_product": "whatsapp", "sync_type": sync_type})
