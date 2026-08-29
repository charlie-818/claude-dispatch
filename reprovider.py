#!/usr/bin/env python3
"""Reassign vault creds that landed as `custom` to their real provider, inferred
from the env-var name. Sets provider slug + proper label; env var / source / secret
untouched. Anything unrecognised stays custom.

Run:  cd /Users/charliebc/claude-dispatch && .venv/bin/python3 reprovider.py
"""
import vault


def main():
    v = vault.load_vault()
    changed, left = [], []
    for cid, r in v.items():
        if r.get("provider") not in (None, "custom"):
            continue
        got = vault.infer_provider(r.get("env_var"))
        if not got:
            left.append(r.get("env_var"))
            continue
        slug, label, _ = got
        r["provider"] = slug
        r["label"] = label
        changed.append((r.get("env_var"), slug))
    vault.save_vault(v)

    print(f"RE-PROVIDERED {len(changed)}")
    for env, slug in sorted(changed):
        print(f"  {env:<34} -> {slug}")
    if left:
        print(f"\nstill custom ({len(left)}): {', '.join(sorted(left))}")


if __name__ == "__main__":
    main()
