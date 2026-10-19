"""Make the three secrets prism-wa needs that nobody hands you.

    python tools/gen_secrets.py

Prints, once, to YOUR terminal (nothing is stored or sent anywhere):

    WA_VERIFY_TOKEN     what Meta echoes back when you subscribe the webhook
    WA_INTERNAL_TOKEN   the shared secret between the licence server and prism-wa (the licence server calls it PRISM_WA_INTERNAL_TOKEN)
    WA_TOKEN_KEYS       encrypts every customer's Meta token. LOSE IT and every stored token is unreadable. Keep a copy outside Render and
                        outside the database (a password manager).

Paste each into Render's dashboard. Do not commit them, and do not paste them into a chat.
Hex and base64url only, so none of them needs quoting anywhere.
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def main() -> int:
    from prism_wa.vault import generate_key
    print("WA_VERIFY_TOKEN=%s" % secrets.token_hex(24))          # 48 characters
    print("WA_INTERNAL_TOKEN=%s" % secrets.token_hex(32))        # 64 characters
    print("WA_TOKEN_KEYS=1:%s" % generate_key())
    print()
    print("Copy WA_TOKEN_KEYS to a password manager NOW. WA_INTERNAL_TOKEN goes on the licence server too, as PRISM_WA_INTERNAL_TOKEN.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
