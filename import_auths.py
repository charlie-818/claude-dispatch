#!/usr/bin/env python3
"""Import a passphrase-encrypted auth bundle (from export_auths.py on another Mac)
into THIS machine's vault. Each secret is re-encrypted at rest with this machine's
own Fernet data key (from its login Keychain) — the bundle's passphrase only
protects it in transit and is never stored.

Owner-run on BigMac:
    cd ~/claude-dispatch && .venv/bin/python3 import_auths.py [path-to-bundle]

Default bundle path: ./auths_bundle.enc . Idempotent — skips env vars already
present. Prints a shred command for the bundle when done.
"""
import base64
import getpass
import json
import os
import sys

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import vault

ITERS = 390000


def key_from(passphrase, salt):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "auths_bundle.enc")
    if not os.path.exists(path):
        print(f"no bundle at {path}"); return
    raw = open(path, "rb").read()
    salt, token = raw[:16], raw[16:]

    pw = getpass.getpass("Bundle passphrase: ")
    try:
        data = Fernet(key_from(pw, salt)).decrypt(token)
        bundle = json.loads(data)
    except Exception:
        print("wrong passphrase or corrupt bundle. aborted."); return

    have = {(vault.get_cred(c) or {}).get("env_var") for c in vault.load_vault()}
    added, skipped = [], []
    for rec in bundle:
        env = rec.get("env_var")
        if env in have:
            skipped.append(env); continue
        vault.add_cred(rec.get("provider", "custom"), rec.get("label"),
                       rec.get("secret"), env, rec.get("scopes") or [])
        have.add(env); added.append(env)

    print(f"imported {len(added)}, skipped {len(skipped)} already present, {len(vault.load_vault())} total")
    for e in sorted(added):
        print(f"  + {e}")
    print(f"\nNow shred the bundle on BOTH machines:\n  rm -P {path}")


if __name__ == "__main__":
    main()
