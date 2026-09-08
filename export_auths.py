#!/usr/bin/env python3
"""Export every vault credential to a passphrase-encrypted bundle for transfer to
another machine. Resolves each live-reference to its current value (the pointers
are local to THIS Mac and wouldn't resolve elsewhere), then encrypts the whole
set under a passphrase you choose.

Security:
  • The bundle is AES (Fernet) encrypted with a key derived from your passphrase
    via PBKDF2-SHA256 (390k iters). The passphrase is never written anywhere.
  • Output file is 0600. Delete it after the import succeeds (the import prints a
    shred command). Transit should still be over the tailnet (encrypted).
  • No secret value is ever printed.

Owner-run (resolving reads secrets, which the daemon is blocked from doing):
    cd /Users/charliebc/claude-dispatch && .venv/bin/python3 export_auths.py
"""
import base64
import getpass
import json
import os

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import vault

OUT = os.path.join(os.path.dirname(__file__), "auths_bundle.enc")
ITERS = 390000


def key_from(passphrase, salt):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def main():
    v = vault.load_vault()
    bundle, skipped = [], []
    for rec in v.values():
        secret = vault.secret_of(rec)
        if not secret:
            skipped.append(rec.get("env_var"))
            continue
        bundle.append({
            "provider": rec.get("provider", "custom"),
            "label": rec.get("label") or rec.get("provider"),
            "env_var": rec.get("env_var"),
            "scopes": rec.get("scopes") or [],
            "secret": secret,
        })

    print(f"resolved {len(bundle)} credentials"
          + (f"; {len(skipped)} unresolvable (skipped): {', '.join(filter(None, skipped))}"
             if skipped else ""))
    if not bundle:
        print("nothing to export."); return

    pw = getpass.getpass("Bundle passphrase (you'll type this again on BigMac): ")
    if len(pw) < 8:
        print("passphrase too short (>= 8). aborted."); return
    if getpass.getpass("Repeat: ") != pw:
        print("mismatch. aborted."); return

    salt = os.urandom(16)
    token = Fernet(key_from(pw, salt)).encrypt(json.dumps(bundle).encode())
    fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, salt + token)   # salt (16B) prefix, then ciphertext
    os.close(fd)
    print(f"\nwrote {OUT} ({len(bundle)} creds, 0600).")
    print("Next: hand the transfer to Claude, or scp it yourself, then run import_auths.py on BigMac.")


if __name__ == "__main__":
    main()
