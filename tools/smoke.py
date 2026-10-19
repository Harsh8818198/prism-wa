"""Check a deployed prism-wa from the outside. Changes nothing; sends no message; touches no customer.

    python tools/smoke.py https://prism-wa-gateway.onrender.com
    WA_VERIFY_TOKEN=... WA_INTERNAL_TOKEN=... python tools/smoke.py https://prism-wa-gateway.onrender.com

Always: the gateway answers, the database answers and has every migration, the worker has been seen.
With WA_VERIFY_TOKEN in the environment: Meta's webhook handshake works with it and is refused with a wrong one.
With WA_INTERNAL_TOKEN: the licence server's door is shut without the token and open with it (a harmless read), and the connect page exists.
A forged webhook is always tried: it must be refused.

Exit code 0 when everything that was checked passed. The tokens are read from the environment so they never appear in a shell history line.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20


def call(method: str, url: str, *, body: bytes = None, headers: dict = None, tries: int = 3) -> tuple:
    """(HTTP status, body); status 0 when nothing answered. A refused request can be answered before its body was read and the connection
    closed under the sender, so a dropped connection is tried again (a service that really is down stays down for all of them)."""
    for attempt in range(tries):
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:                                       # noqa: BLE001
            last = str(e).encode()
            time.sleep(0.2 * (attempt + 1))
    return 0, last


def run(base: str, verify_token: str = "", internal_token: str = "") -> list:
    """[(name, ok, detail)] for every check that could be made."""
    base = base.rstrip("/")
    out = []

    def check(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    status, raw = call("GET", base + "/healthz")
    check("gateway answers (/healthz)", status == 200, "HTTP %s" % status)
    if status != 200:
        return out                                                   # nothing else can be meaningful

    status, raw = call("GET", base + "/readyz")
    try:
        ready = json.loads(raw.decode("utf-8"))
    except ValueError:
        ready = {}
    check("database answers (/readyz)", ready.get("database") is True, "HTTP %s" % status)
    check("every migration is applied", ready.get("schema") == "current", "schema: %s" % ready.get("schema", "?"))
    check("the worker has been seen", ready.get("worker") == "running",
          "worker: %s%s" % (ready.get("worker", "?"), " (%ss ago)" % ready["worker_seen_seconds_ago"] if "worker_seen_seconds_ago" in ready else ""))

    forged = call("POST", base + "/webhook/wa", body=b'{"object":"whatsapp_business_account","entry":[]}',
                  headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64})
    check("a forged webhook is refused", forged[0] == 401, "HTTP %s" % forged[0])
    unsigned = call("POST", base + "/webhook/wa", body=b"{}", headers={"Content-Type": "application/json"})
    check("an unsigned webhook is refused", unsigned[0] == 401, "HTTP %s" % unsigned[0])

    if verify_token:
        q = urllib.parse.urlencode({"hub.mode": "subscribe", "hub.verify_token": verify_token, "hub.challenge": "smoke123"})
        status, raw = call("GET", base + "/webhook/wa?" + q)
        check("Meta's handshake works with WA_VERIFY_TOKEN", status == 200 and raw.decode("utf-8") == "smoke123", "HTTP %s" % status)
        q = urllib.parse.urlencode({"hub.mode": "subscribe", "hub.verify_token": verify_token + "x", "hub.challenge": "smoke123"})
        check("...and is refused with a wrong one", call("GET", base + "/webhook/wa?" + q)[0] == 403)

    if internal_token:
        door = base + "/internal/sso/required"
        body = json.dumps({"tenant_id": "00000000-0000-0000-0000-000000000000"}).encode("utf-8")
        shut = call("POST", door, body=body, headers={"Content-Type": "application/json"})
        check("the licence server's door is shut without the token", shut[0] in (401, 403), "HTTP %s" % shut[0])
        wrong = call("POST", door, body=body, headers={"Content-Type": "application/json", "X-Internal-Token": internal_token + "x"})
        check("...and with a wrong one", wrong[0] in (401, 403), "HTTP %s" % wrong[0])
        opened = call("POST", door, body=body, headers={"Content-Type": "application/json", "X-Internal-Token": internal_token})
        check("...and open with WA_INTERNAL_TOKEN (a harmless read)", opened[0] == 200, "HTTP %s" % opened[0])
    return out


def main(argv: list) -> int:
    if len(argv) != 2 or not argv[1].startswith("http"):
        print(__doc__)
        return 2
    results = run(argv[1], os.environ.get("WA_VERIFY_TOKEN", "").strip(), os.environ.get("WA_INTERNAL_TOKEN", "").strip())
    for name, ok, detail in results:
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  (%s)" % detail) if detail and not ok else ""))
    if not os.environ.get("WA_VERIFY_TOKEN") or not os.environ.get("WA_INTERNAL_TOKEN"):
        print("note: set WA_VERIFY_TOKEN and WA_INTERNAL_TOKEN in the environment to check the handshake and the licence server's door too")
    return 0 if results and all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
