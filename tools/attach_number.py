"""Connect a number that is ALREADY live on Meta (registered and working) to a prism-wa workspace, without Meta's pop-up.

    python tools/attach_number.py https://prism-wa.onrender.com --tenant <workspace id> --waba <WhatsApp Business Account id> --number <phone number id>
    add --coexistence when the number is also still used in the WhatsApp Business app on the phone.

It asks for two secrets and never prints them or keeps them in a shell history line:
  * the internal token (WA_INTERNAL_TOKEN, the same value the licence server holds), and
  * an access token for the account: Meta Business settings -> Users -> System users -> (add one, give it the WhatsApp account with full control)
    -> Generate token, app "Prism", permissions whatsapp_business_management and whatsapp_business_messaging, "Never" expires.
The server checks that the token really reaches the account and that the number is on it, stores the token encrypted, and subscribes the app. Nothing is
sent to anyone and no message is sent. Exit code 0 when it connected.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("url", help="the prism-wa address, e.g. https://prism-wa.onrender.com")
    ap.add_argument("--tenant", required=True, help="the workspace id")
    ap.add_argument("--waba", required=True, help="the WhatsApp Business Account id (Meta calls it the Messaging Account ID)")
    ap.add_argument("--number", required=True, help="the phone number id (not the number itself)")
    ap.add_argument("--coexistence", action="store_true", help="the number is also used in the WhatsApp Business app on the phone")
    args = ap.parse_args(argv)

    internal = getpass.getpass("Internal token (hidden): ").strip()
    access = getpass.getpass("Access token for the account (hidden): ").strip()
    if not (internal and access):
        print("Both tokens are needed.")
        return 2
    body = json.dumps({"tenant_id": args.tenant, "waba_id": args.waba, "phone_number_id": args.number,
                       "access_token": access, "coexistence": args.coexistence}).encode("utf-8")
    req = urllib.request.Request(args.url.rstrip("/") + "/internal/onboarding/attach", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Internal-Token": internal})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:                      # noqa: S310 (the address the owner typed)
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        print("Could not reach the server: %s" % exc)
        return 1
    try:
        out = json.loads(raw.decode("utf-8"))
    except ValueError:
        out = {}
    if status == 200 and out.get("ok"):
        print("Connected: account %s, number %s. Open Prism -> WhatsApp -> Settings -> Connection." % (out.get("waba_id"), out.get("phone_number_id")))
        return 0
    print("Not connected (HTTP %s): %s" % (status, out.get("message") or out.get("error") or "no explanation"))
    for e in out.get("errors") or []:
        print("  - %s: %s" % (e.get("step"), e.get("message")))
    return 1


if __name__ == "__main__":
    sys.exit(main())
