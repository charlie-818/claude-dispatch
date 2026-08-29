#!/usr/bin/env python3
"""Pull the env vars stored inside every Vercel + Netlify project and register the
secret ones as live references. Those platforms hold the *real* keys a deploy uses
— Resend, database URLs, Stripe, third-party APIs — so they're a dense source of
auths an agent can be granted.

Live-reference model: we store only a pointer (project/site + key). At grant time
the daemon calls the platform API with your already-registered account token and
returns the current value — never stored, always fresh, follows rotation. Values
are inspected here only to skip empties and to guess the provider (for the logo);
no value is ever printed.

Owner-run (the harness blocks the daemon from reading these):
    cd /Users/charliebc/claude-dispatch && .venv/bin/python3 scan_deploy_env.py
"""
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import vault
from sweep_auth import classify, SECRETY_NAME, mask

# Vercel/Netlify inject their own non-secret build vars — skip the noise.
SKIP = {"VERCEL", "CI", "NODE_ENV", "NEXT_RUNTIME", "TURBO_REMOTE_ONLY", "NX_DAEMON"}
SKIP_PREFIX = ("VERCEL_", "NEXT_PUBLIC_", "NETLIFY_", "PUBLIC_", "VITE_", "NX_", "TURBO_")


def api(url, bearer, params=None):
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"    ! api {url.split('?')[0]} -> {type(e).__name__}: {e}")
        return None


def secret_key(k):
    if k in SKIP or k.startswith(SKIP_PREFIX):
        return False
    return bool(SECRETY_NAME.search(k))


def main():
    have = {(vault.get_cred(c) or {}).get("env_var")
            for c in vault.load_vault() if (vault.get_cred(c) or {}).get("source")}
    reg = {}      # env_var -> (provider, source, where)
    seen_names = set()

    def offer(key, value, source, where):
        if key in have or key in reg or key in seen_names:
            return
        seen_names.add(key)
        prov = classify(key, value) or "custom"
        reg[key] = (prov, source, where)

    # ── Vercel ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70 + "\nVERCEL PROJECTS\n" + "=" * 70)
    vtok = None
    p = Path(os.path.expanduser("~/Library/Application Support/com.vercel.cli/auth.json"))
    if p.exists():
        try:
            vtok = json.loads(p.read_text()).get("token")
        except Exception:
            pass
    if not vtok:
        print("  no Vercel token (auth.json) — run `vercel login` first, skipping")
    else:
        scopes = [{"teamId": None, "name": "personal"}]
        teams = api("https://api.vercel.com/v2/teams", vtok, {"limit": "100"}) or {}
        for t in teams.get("teams", []):
            scopes.append({"teamId": t.get("id"), "name": t.get("slug") or t.get("name")})
        for sc in scopes:
            projs = api("https://api.vercel.com/v9/projects", vtok,
                        {"limit": "100", "teamId": sc["teamId"]}) or {}
            plist = projs.get("projects", [])
            if plist:
                print(f"\n  scope: {sc['name']}  ({len(plist)} projects)")
            for pr in plist:
                envd = api(f"https://api.vercel.com/v9/projects/{pr['id']}/env", vtok,
                           {"decrypt": "true", "teamId": sc["teamId"]}) or {}
                keys = []
                for e in envd.get("envs", []):
                    k, val = e.get("key", ""), e.get("value")
                    if not secret_key(k) or not val:
                        continue
                    keys.append(k)
                    offer(k, val, {"kind": "vercel_env", "project": pr["id"],
                                   "key": k, "team": sc["teamId"]}, f"vercel/{pr['name']}")
                if keys:
                    print(f"    {pr['name']:<28} {', '.join(keys[:8])}"
                          f"{' …' if len(keys) > 8 else ''}")

    # ── Netlify ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 70 + "\nNETLIFY SITES\n" + "=" * 70)
    ntok = None
    p = Path(os.path.expanduser("~/Library/Preferences/netlify/config.json"))
    if p.exists():
        try:
            users = json.loads(p.read_text()).get("users", {})
            for u in users.values():
                ntok = ((u.get("auth") or {}).get("token")) or u.get("token")
                if ntok:
                    break
        except Exception:
            pass
    if not ntok:
        print("  no Netlify token (config.json) — run `netlify login` first, skipping")
    else:
        sites = api("https://api.netlify.com/api/v1/sites", ntok, {"per_page": "100"}) or []
        for s in sites:
            acct, sid, name = s.get("account_slug"), s.get("id"), s.get("name")
            if not acct:
                continue
            envd = api(f"https://api.netlify.com/api/v1/accounts/{acct}/env", ntok,
                       {"site_id": sid}) or []
            keys = []
            for e in envd if isinstance(envd, list) else []:
                k = e.get("key", "")
                val = next((v.get("value") for v in e.get("values", []) if v.get("value")), None)
                if not secret_key(k) or not val:
                    continue
                keys.append(k)
                offer(k, val, {"kind": "netlify_env", "account": acct, "site": sid, "key": k},
                      f"netlify/{name}")
            if keys:
                print(f"  {name:<30} {', '.join(keys[:8])}{' …' if len(keys) > 8 else ''}")

    # ── register ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 70 + "\nREGISTERING (dedup by var name, first project wins)\n" + "=" * 70)
    if not reg:
        print("  nothing new found.")
    for key, (prov, source, where) in sorted(reg.items()):
        label = vault.PROVIDERS.get(prov, {}).get("label", prov)
        try:
            cid = vault.add_source_cred(prov, f"{label} — {key}", key, source, [])
            print(f"  + {key:<28} [{prov}]  from {where}")
        except Exception as e:
            print(f"  ! {key:<28} FAILED: {type(e).__name__}: {e}")

    print("\n── VAULT now holds " + "─" * 50)
    for c in vault.list_creds():
        print(f"  {c['provider']:<14} {c['env_var']:<28} live={c.get('live')}")
    print(f"\nDone — {len(reg)} new. Tell Claude to restart the daemon + verify one"
          " resolves live.\n")


if __name__ == "__main__":
    main()
