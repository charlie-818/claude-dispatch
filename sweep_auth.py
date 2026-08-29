#!/usr/bin/env python3
"""Border sweep v2 — signature-based credential discovery for dispatch agents.

The old version walked a fixed list of ~27 CLIs. That misses everything that
isn't a CLI login — Resend, SendGrid, OpenAI, a raw DB URL in a project `.env`,
any `*_API_KEY`. This version is a *finder*: it enumerates every place a secret
can hide on this Mac, then identifies each one two ways —

  1. by VALUE fingerprint  (`re_…` = Resend, `sk-ant-…` = Anthropic, `xoxb-…` = Slack)
  2. by NAME               (`RESEND_API_KEY`, `*_TOKEN`, `*_API_KEY`)

Anything it can name → registered as a live reference (pointer only, never the
value). Anything secret-shaped but unrecognised → listed as UNKNOWN so you can
label it. New providers get a real logo automatically (vault registry carries
each one's domain; the UI paints the favicon).

Owner-run (the harness blocks the daemon from reading local creds):
    cd /Users/charliebc/claude-dispatch && .venv/bin/python3 sweep_auth.py

No secret VALUE is ever printed — only names, lengths, last-4, and file paths.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import vault

HOME = Path.home()


def sh(argv, timeout=8):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return 127, "", f"{type(e).__name__}: {e}"


def have(cmd):
    return shutil.which(cmd) is not None


def mask(v):
    v = v or ""
    return f"len {len(v)}, …{v[-4:]}" if len(v) >= 4 else f"len {len(v)}"


# ── VALUE fingerprints — how a secret's own text identifies its issuer ────────
# (provider, compiled regex). Matched against values; values never printed.
SIGS = [
    ("resend",      r"^re_[A-Za-z0-9]{16,}$"),
    ("anthropic",   r"^sk-ant-[A-Za-z0-9_\-]{20,}$"),
    ("openai",      r"^sk-(proj-)?[A-Za-z0-9_\-]{20,}$"),
    ("github",      r"^gh[pousr]_[A-Za-z0-9]{20,}$"),
    ("stripe",      r"^(sk|rk|pk)_(live|test)_[A-Za-z0-9]{16,}$"),
    ("sendgrid",    r"^SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}$"),
    ("slack",       r"^xox[baprs]-[A-Za-z0-9\-]{10,}$"),
    ("google",      r"^AIza[A-Za-z0-9_\-]{30,}$"),
    ("aws",         r"^(AKIA|ASIA)[A-Z0-9]{16}$"),
    ("linear",      r"^lin_api_[A-Za-z0-9]{20,}$"),
    ("notion",      r"^(secret_|ntn_)[A-Za-z0-9]{20,}$"),
    ("huggingface", r"^hf_[A-Za-z0-9]{20,}$"),
    ("fly",         r"^(FlyV1|fo1_)[A-Za-z0-9_\-\.]{10,}$"),
    ("planetscale", r"^pscale_tkn_[A-Za-z0-9_\-]{20,}$"),
    ("groq",        r"^gsk_[A-Za-z0-9]{20,}$"),
    ("perplexity",  r"^pplx-[A-Za-z0-9]{20,}$"),
    ("elevenlabs",  r"^sk_[a-f0-9]{40,}$"),
    ("replicate",   r"^r8_[A-Za-z0-9]{20,}$"),
    ("posthog",     r"^ph[cx]_[A-Za-z0-9]{20,}$"),
    ("twilio",      r"^SK[0-9a-f]{32}$"),
    ("ngrok",       r"^[0-9][A-Za-z0-9]{20,}_[A-Za-z0-9]{20,}$"),
    ("doppler",     r"^dp\.pt\.[A-Za-z0-9]{20,}$"),
]
SIGS = [(p, re.compile(rx)) for p, rx in SIGS]

# env-var name → provider (built from the vault registry, so it tracks _EXTRA).
ENV2PROV = {}
for _prov, _meta in vault.PROVIDERS.items():
    ev = _meta.get("env_var")
    if ev:
        ENV2PROV.setdefault(ev, _prov)

# loose NAME hints — when the exact env var differs but the word is in there.
NAME_HINTS = [(re.compile(r, re.I), p) for r, p in [
    (r"resend", "resend"), (r"sendgrid|SG_", "sendgrid"), (r"postmark", "postmark"),
    (r"perplexity|pplx", "perplexity"), (r"mistral", "mistral"), (r"cohere", "cohere"),
    (r"elevenlabs|eleven_labs", "elevenlabs"), (r"replicate", "replicate"),
    (r"mailgun", "mailgun"), (r"loops", "loops"), (r"twilio", "twilio"),
    (r"openai", "openai"), (r"anthropic|claude", "anthropic"), (r"groq", "groq"),
    (r"stripe", "stripe"), (r"slack", "slack"), (r"linear", "linear"),
    (r"notion", "notion"), (r"cloudflare|^CF_", "cloudflare"), (r"fly[_-]?io|^FLY_", "fly"),
    (r"heroku", "heroku"), (r"digitalocean|^DO_", "digitalocean"), (r"vercel", "vercel"),
    (r"netlify", "netlify"), (r"railway", "railway"), (r"supabase", "supabase"),
    (r"github|^GH_", "github"), (r"gitlab", "gitlab"), (r"sentry", "sentry"),
    (r"ngrok", "ngrok"), (r"neon", "neon"), (r"planetscale|pscale", "planetscale"),
    (r"turso", "turso"), (r"upstash", "upstash"), (r"huggingface|^HF_", "huggingface"),
    (r"datadog|^DD_", "datadog"), (r"mongodb|mongo_atlas", "mongodb"),
    (r"posthog", "posthog"), (r"expo|^EAS_", "expo"), (r"firebase", "firebase"),
    (r"circleci", "circleci"), (r"npm", "npm"),
]]

# a value looks secret-shaped (for surfacing UNKNOWNs worth labelling)
SECRETY_NAME = re.compile(r"(TOKEN|API[_-]?KEY|APIKEY|SECRET|ACCESS[_-]?KEY|AUTH|"
                          r"CLIENT[_-]?SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY|"
                          r"_KEY$|^KEY_|BEARER|CREDENTIAL|WEBHOOK|SIGNING|"
                          r"DATABASE_URL|_DSN|CONNECTION[_-]?STRING)", re.I)
# obviously-not-a-secret names, even if they contain a hint word (env noise, paths)
NOT_SECRET = re.compile(r"^(NODE_ENV|ENV|ENVIRONMENT|PORT|HOST|HOSTNAME|URL|BASE_URL|"
                        r"PUBLIC_URL|LOG_LEVEL|DEBUG|REGION|VERSION|NAME|EMAIL|"
                        r"USERNAME|USER|LOGNAME|TIMEOUT|LOCALE|TZ|PATH|HOME|SHELL|"
                        r"LANG|PWD|OLDPWD|SHLVL|TERM|TERM_.*|TERMCAP|COLORTERM|"
                        r"COLORFGBG|LSCOLORS|LS_COLORS|SSH_AUTH_SOCK|SSH_.*_SOCK|"
                        r"__CF.*|XPC_.*|SECURITYSESSIONID|DISPLAY|EDITOR|PAGER|"
                        r"MANPATH|INFOPATH|CONDA_.*|VIRTUAL_ENV|npm_.*)$", re.I)
_CONN = re.compile(r"^[a-zA-Z][\w+.\-]*://[^/\s]*:[^/\s@]+@", re.S)   # creds inside a URL
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-\.=/+]{16,}$")
# values that are plainly not secrets: filesystem paths, sockets, bare URLs
_NONSECRET_VAL = re.compile(r"^(/|~|\./|\.\./|[a-z]:\\)|\.sock$", re.I)


def looks_secret(name, value):
    """True when (name, value) is worth registering. Value inspected, never printed."""
    name = name or ""
    if not value or NOT_SECRET.match(name):
        return False
    if any(rx.match(value) for _, rx in SIGS):          # known key fingerprint
        return True
    if _CONN.match(value):                              # user:pass@host connection string
        return True
    # reject filesystem paths / sockets / bare URLs (no embedded creds)
    if _NONSECRET_VAL.search(value) or value.startswith(("http://", "https://")):
        return False
    if SECRETY_NAME.search(name):                       # secret-ish name + non-trivial value
        return " " not in value and len(value) >= 8
    # unnamed but high-entropy tokenish value (mixed alnum, no spaces, long)
    return (_TOKENISH.match(value) and len(value) >= 24
            and bool(re.search(r"\d", value)) and bool(re.search(r"[A-Za-z]", value)))


def classify(name, value):
    """(provider or None) for a discovered secret. Value inspected, never printed."""
    for prov, rx in SIGS:
        if value and rx.match(value):
            return prov
    if name in ENV2PROV:
        return ENV2PROV[name]
    for rx, prov in NAME_HINTS:
        if rx.search(name):
            return prov
    got = vault.infer_provider(name)     # real service from the var name
    return got[0] if got else None


# ── CLI-login tools: config-file / command sources (not env-var backed) ───────
#   provider, label, cli, probe, source
CLI_CATALOG = [
    ("github",   "gh",       ("cmd", ["gh", "auth", "status"]),
        {"kind": "cmd", "argv": ["gh", "auth", "token"]}),
    ("vercel",   "vercel",   ("file", "~/Library/Application Support/com.vercel.cli/auth.json"),
        {"kind": "json", "path": "~/Library/Application Support/com.vercel.cli/auth.json", "keys": ["token"]}),
    ("netlify",  "netlify",  ("file", "~/Library/Preferences/netlify/config.json"),
        {"kind": "json_user", "path": "~/Library/Preferences/netlify/config.json"}),
    ("railway",  "railway",  ("file", "~/.railway/config.json"),
        {"kind": "json", "path": "~/.railway/config.json", "keys": ["user", "token"]}),
    ("supabase", "supabase", ("file", "~/.supabase/access-token"),
        {"kind": "file", "path": "~/.supabase/access-token"}),
    ("gcloud",   "gcloud",   ("cmd", ["gcloud", "auth", "list", "--format=value(account)"]),
        {"kind": "cmd", "argv": ["gcloud", "auth", "print-access-token"]}),
    ("fly",      "flyctl",   ("cmd", ["flyctl", "auth", "token"]),
        {"kind": "cmd", "argv": ["flyctl", "auth", "token"]}),
    ("heroku",   "heroku",   ("netrc", "api.heroku.com"),
        {"kind": "netrc", "machine": "api.heroku.com"}),
    ("doctl",    "doctl",    ("file", "~/Library/Application Support/doctl/config.yaml"),
        {"kind": "yaml", "path": "~/Library/Application Support/doctl/config.yaml", "keys": ["access-token"]}),
    ("npm",      "npm",      ("npmrc", "~/.npmrc"),
        {"kind": "npmrc", "path": "~/.npmrc"}),
    ("firebase", "firebase", ("file", "~/.config/configstore/firebase-tools.json"),
        {"kind": "json", "path": "~/.config/configstore/firebase-tools.json", "keys": ["tokens", "refresh_token"]}),
    ("huggingface", "huggingface-cli", ("file", "~/.cache/huggingface/token"),
        {"kind": "file", "path": "~/.cache/huggingface/token"}),
    ("neon",     "neonctl",  ("file", "~/.config/neonctl/credentials.json"),
        {"kind": "json", "path": "~/.config/neonctl/credentials.json", "keys": ["access_token"]}),
    ("turso",    "turso",    ("file", "~/.config/turso/settings.json"),
        {"kind": "json", "path": "~/.config/turso/settings.json", "keys": ["token"]}),
    ("expo",     "eas",      ("file", "~/.expo/state.json"),
        {"kind": "json", "path": "~/.expo/state.json", "keys": ["auth", "sessionSecret"]}),
    ("circleci", "circleci", ("file", "~/.circleci/cli.yml"),
        {"kind": "yaml", "path": "~/.circleci/cli.yml", "keys": ["token"]}),
    ("sentry",   "sentry-cli", ("file", "~/.sentryclirc"), None),
]


def cli_authed(probe, cli):
    kind = probe[0]
    if kind == "cmd":
        return sh(probe[1])[0] == 0
    if kind == "file":
        return Path(os.path.expanduser(probe[1])).exists()
    if kind == "netrc":
        n = HOME / ".netrc"
        return n.exists() and probe[1] in n.read_text()
    if kind == "npmrc":
        p = Path(os.path.expanduser(probe[1]))
        return p.exists() and "_authToken" in p.read_text()
    return False


def parse_kv(text):
    """Yield (name, value) from shell/.env text — export FOO=bar, FOO="bar"."""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", s)
        if not m:
            continue
        val = m.group(2).strip()
        if val and val[0] in "\"'" and val[-1:] == val[0]:
            val = val[1:-1]
        yield m.group(1), val


def main():
    print("\n" + "=" * 72)
    print("BORDER SWEEP v2 — signature-based credential discovery")
    print("=" * 72)

    have_env = {(vault.get_cred(c) or {}).get("env_var")
                for c in vault.load_vault()
                if (vault.get_cred(c) or {}).get("source")}

    # candidate registrations, keyed by env_var so we register each once
    reg = {}          # env_var -> (provider, source, origin)
    identified = []   # (provider, name, origin, masked)

    def offer(provider, env_var, source, origin):
        if env_var in have_env or env_var in reg:
            return
        reg[env_var] = (provider, source, origin)

    # 1) CLI logins ------------------------------------------------------------
    cli_rows = []
    for prov, cli, probe, source in CLI_CATALOG:
        installed = have(cli)
        authed = cli_authed(probe, cli) if installed else False
        cli_rows.append((prov, cli, installed, authed))
        if authed and source:
            env_var = vault.PROVIDERS.get(prov, {}).get("env_var") or f"{prov.upper()}_TOKEN"
            offer(prov, env_var, source, f"{cli} login")

    dupes = []        # (env_var, origin) — same var seen again with a different source

    def take(name, value, origin, source):
        """Register any secret-shaped (name, value). Unknown provider → custom."""
        if not looks_secret(name, value):
            return
        prov = classify(name, value) or "custom"
        # env / dotenv resolve by the literal var name, so key on it; CLI-backed
        # providers keep their canonical env var.
        env_var = name
        identified.append((prov, name, origin, mask(value)))
        if env_var in have_env:
            return
        if env_var in reg:
            dupes.append((env_var, origin))
            return
        reg[env_var] = (prov, source, origin)

    # 2) live environment ------------------------------------------------------
    for name, value in os.environ.items():
        take(name, value, "env", {"kind": "env", "name": name})

    # 3) shell profiles + top-level dotfiles -----------------------------------
    for prof in (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile",
                 ".profile", ".env", ".envrc", ".config/fish/config.fish"):
        p = HOME / prof
        if not p.exists():
            continue
        try:
            text = p.read_text()
        except Exception:
            continue
        for name, value in parse_kv(text):
            # read straight from the profile file at resolve time — more reliable
            # than env (an interactive-only export may never reach the daemon).
            take(name, value, prof, {"kind": "dotenv", "path": str(p), "key": name})

    # 4) project .env files — now REGISTERED via the dotenv resolver ------------
    roots = [HOME, HOME / "Documents", HOME / "Desktop", HOME / "code", HOME / "Code",
             HOME / "projects", HOME / "Projects", HOME / "src", HOME / "dev",
             HOME / "Developer", HOME / "repos", HOME / "git", HOME / "work"]
    ENV_GLOBS = ("*/.env", "*/.env.local", "*/.env.*", "*/*/.env",
                 "*/*/.env.local", "*/*/.env.*")
    SKIP = (".venv", "node_modules", "/.git/", "site-packages", "dist/", "build/",
            ".env.example", ".env.sample", ".env.template")
    dotenvs = []
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for glb in ENV_GLOBS:
            for f in root.glob(glb):
                sf = str(f)
                if f in seen or any(k in sf for k in SKIP):
                    continue
                seen.add(f)
                try:
                    kvs = list(parse_kv(f.read_text()))
                except Exception:
                    continue
                hit_names = []
                for name, value in kvs:
                    if looks_secret(name, value):
                        hit_names.append(name)
                        take(name, value, f"…/{f.parent.name}/{f.name}",
                             {"kind": "dotenv", "path": sf, "key": name})
                if hit_names:
                    dotenvs.append((sf, hit_names))

    # ── report ────────────────────────────────────────────────────────────────
    print("\n── CLI LOGINS " + "─" * 57)
    for prov, cli, inst, authed in cli_rows:
        tag = "✔ authed" if authed else ("· not logged in" if inst else "✗ absent")
        print(f"  {tag:<16} {prov} ({cli})")

    known = sorted({(p, n, o, m) for p, n, o, m in identified if p != "custom"})
    custom = sorted({(p, n, o, m) for p, n, o, m in identified if p == "custom"})
    print(f"\n── IDENTIFIED KEYS — {len(known)} named " + "─" * 40)
    if not known:
        print("  none")
    for prov, name, origin, m in known:
        print(f"  {prov:<12} ${name:<28} [{origin}]  {m}")

    if custom:
        print(f"\n── UNRECOGNISED but secret-shaped — {len(custom)} (registered as custom) " + "─" * 6)
        for _, name, origin, m in custom:
            print(f"  ?  ${name:<28} [{origin}]  {m}")

    if dupes:
        print(f"\n── COLLISIONS — {len(dupes)} vars seen again elsewhere (only first registered) " + "─" * 2)
        for env_var, origin in sorted(set(dupes)):
            print(f"  ~  ${env_var:<28} also in [{origin}]")

    if dotenvs:
        print(f"\n── PROJECT .env FILES scanned — {len(dotenvs)} " + "─" * 33)
        for path, names in sorted(dotenvs)[:60]:
            print(f"  {path}  ({len(names)} secrets)")
        if len(dotenvs) > 60:
            print(f"  … +{len(dotenvs) - 60} more")

    # keychain service labels of interest
    rc, out, _ = sh(["security", "dump-keychain"], timeout=25)
    if rc == 0 and out:
        svcs = sorted(set(re.findall(r'"svce"<blob>="([^"]+)"', out)))
        hits = [s for s in svcs if re.search(r"(token|api|oauth|secret|\.com|\.dev|\.io|cli)", s, re.I)]
        if hits:
            print("\n── KEYCHAIN services of interest (labels only) " + "─" * 22)
            for s in hits[:80]:
                print(f"  {s}")
            if len(hits) > 80:
                print(f"  … +{len(hits) - 80} more")

    # ── register ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("REGISTERING live-references (pointers only, idempotent)")
    print("=" * 72)
    if not reg:
        print("  nothing new — everything discovered is already in the vault.")
    for env_var, (prov, source, origin) in sorted(reg.items()):
        label = vault.PROVIDERS.get(prov, {}).get("label", prov)
        scopes = vault.PROVIDERS.get(prov, {}).get("scopes", [])
        try:
            cid = vault.add_source_cred(prov, label, env_var, source, scopes)
            print(f"  + {label:<16} {env_var:<26} via {origin:<12} -> {cid}")
        except Exception as e:
            print(f"  ! {label:<16} {env_var:<26} FAILED: {type(e).__name__}: {e}")

    print("\n── VAULT now holds " + "─" * 52)
    for c in vault.list_creds():
        print(f"  {c['provider']:<14} {c['env_var']:<26} live={c.get('live')}")

    print("\nDone. Tell Claude — it restarts the daemon (loads new resolver + logos)")
    print("and verifies one new tool resolves live. UNKNOWNs above: tell Claude the")
    print("provider and it'll add a signature so the next sweep names them.\n")


if __name__ == "__main__":
    main()
