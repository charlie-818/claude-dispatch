"""Agent auth broker for CC Dispatch — encrypted credential vault + grants.

The daemon can already type into panes that hold every credential on the machine,
so a store of *service* secrets (GitHub, Vercel, a database URL, …) raises the
stakes considerably. The design keeps a secret in exactly three places and no
others: encrypted on disk, decrypted in this process's memory, and — only at the
moment an owner-approved grant is redeemed — in the requesting agent's own stdout.

Layers
------
1. Secrets are encrypted at rest with Fernet (AES-128-CBC + HMAC). The data key
   is NOT on disk: it lives in the login Keychain, fetched once at first use via
   the `security` CLI. The `.integrations.vault` file is ciphertext only.
2. Nothing is ever returned to the browser but metadata + a masked last-4
   (`redact`). The plaintext leaves this module only through `release()`, and
   only for a pane that holds a live, owner-created grant.
3. Requests and grants (`.grants.json`) carry NO secret — only pane id, service,
   scopes and timestamps — so that file is merely sensitive, not catastrophic.

The trust anchor is the grant: an agent can *ask* freely, but a secret is
released only after the owner approves a grant through the passkey-gated phone UI
(see server.py `/api/requests/{id}/approve`).
"""
import base64, json, os, pathlib, secrets, subprocess, time

from cryptography.fernet import Fernet, InvalidToken

HERE = pathlib.Path(__file__).parent
VAULT_FILE = HERE / ".integrations.vault"      # Fernet ciphertext, 0600, gitignored
GRANTS_FILE = HERE / ".grants.json"            # non-secret metadata, 0600, gitignored

# Keychain coordinates for the data key. A single innocuous item; rotating it (or
# deleting it) invalidates the whole vault at once — the panic button.
KC_SERVICE = "cc-dispatch-vault"
KC_ACCOUNT = "datakey"

REQUEST_TTL = 3600          # a pending request older than this is stale
GRANT_TTL_DEFAULT = None    # grants don't expire unless the owner sets one


# ── provider registry ───────────────────────────────────────────────────────
# Open-ended on purpose: "custom" lets the owner name any env var (npm, Docker,
# a DB URL, a cloud key) so the set grows with what the agents actually need.
PROVIDERS = {
    "github":   {"label": "GitHub",   "color": "#f2f5f9", "domain": "github.com",
                 "env_var": "GITHUB_TOKEN",
                 "help": "https://github.com/settings/tokens?type=beta",
                 "scopes": ["contents", "pull_requests", "issues", "workflow", "read:org"],
                 "device_flow": True},
    "vercel":   {"label": "Vercel",   "color": "#66a9ff", "domain": "vercel.com",
                 "env_var": "VERCEL_TOKEN",
                 "help": "https://vercel.com/account/tokens",
                 "scopes": ["deploy", "read", "env"], "device_flow": False},
    "netlify":  {"label": "Netlify",  "color": "#5bd6cd", "domain": "netlify.com",
                 "env_var": "NETLIFY_AUTH_TOKEN",
                 "help": "https://app.netlify.com/user/applications#personal-access-tokens",
                 "scopes": ["deploy", "sites", "dns"], "device_flow": False},
    "supabase": {"label": "Supabase", "color": "#3ddc5c", "domain": "supabase.com",
                 "env_var": "SUPABASE_ACCESS_TOKEN",
                 "help": "https://supabase.com/dashboard/account/tokens",
                 "scopes": ["projects:read", "projects:write"], "device_flow": False},
    "railway":  {"label": "Railway",  "color": "#bd8dff", "domain": "railway.app",
                 "env_var": "RAILWAY_TOKEN",
                 "help": "https://railway.app/account/tokens",
                 "scopes": ["project", "deploy"], "device_flow": False},
    "custom":   {"label": "Custom",   "color": "#a8b2bf", "domain": None,
                 "env_var": None, "help": "", "scopes": [], "device_flow": False},
}


def providers_public():
    """Registry for the add-form — no secrets, just shape."""
    return {k: {kk: v[kk] for kk in ("label", "color", "domain", "env_var", "help",
                                     "scopes", "device_flow")}
            for k, v in PROVIDERS.items()}


# ── data key (Keychain) ──────────────────────────────────────────────────────
_key_cache = None


def vault_key():
    """Fetch (or, on first use, create) the Fernet data key in the login Keychain.

    Runs prompt-free when the daemon is a LaunchAgent in the login session. The
    key is cached in memory so we hit `security` at most once per process.
    """
    global _key_cache
    if _key_cache:
        return _key_cache
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KC_SERVICE, "-a", KC_ACCOUNT, "-w"],
            capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            _key_cache = r.stdout.strip().encode()
            return _key_cache
    except Exception as e:
        print(f"  [vault] keychain read failed: {type(e).__name__}: {e}", flush=True)
    # First run: mint a key and store it. -U updates in place if it somehow exists.
    key = Fernet.generate_key()
    try:
        subprocess.run(
            ["security", "add-generic-password", "-s", KC_SERVICE, "-a", KC_ACCOUNT,
             "-w", key.decode(), "-U"],
            capture_output=True, text=True, check=True)
        print(f"  [vault] created keychain data key ({KC_SERVICE})", flush=True)
    except Exception as e:
        # No Keychain (headless CI, etc.): fall back to an on-disk key so the app
        # still runs. Shouted about because it drops encryption-at-rest to
        # obfuscation. Never the intended path on the owner's Mac.
        print(f"  [vault] KEYCHAIN UNAVAILABLE ({e}); using on-disk key — NOT secure",
              flush=True)
        kf = HERE / ".vault.key"
        if kf.exists():
            key = kf.read_bytes().strip()
        else:
            fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd, key)
            os.close(fd)
    _key_cache = key
    return key


def _fernet():
    return Fernet(vault_key())


# ── vault store ──────────────────────────────────────────────────────────────
def load_vault():
    try:
        blob = VAULT_FILE.read_bytes()
        if not blob:
            return {}
        return json.loads(_fernet().decrypt(blob).decode())
    except FileNotFoundError:
        return {}
    except InvalidToken:
        print("  [vault] cannot decrypt vault — wrong/rotated key", flush=True)
        return {}
    except Exception as e:
        print(f"  [vault] load failed: {type(e).__name__}: {e}", flush=True)
        return {}


def save_vault(v):
    blob = _fernet().encrypt(json.dumps(v).encode())
    tmp = str(VAULT_FILE) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, blob)
    os.close(fd)
    os.replace(tmp, VAULT_FILE)               # atomic; no half-written ciphertext
    os.chmod(VAULT_FILE, 0o600)


def _id():
    return secrets.token_urlsafe(8)


def _last4(s):
    return s[-4:] if isinstance(s, str) and len(s) >= 4 else "····"


def env_var_for(provider, override=None):
    if override:
        return override
    return (PROVIDERS.get(provider) or PROVIDERS["custom"]).get("env_var") or "API_TOKEN"


def add_cred(provider, label, secret, env_var=None, scopes=None, expires=None):
    """Store a credential. Returns its id. `secret` is encrypted, never logged."""
    if not secret:
        raise ValueError("empty secret")
    v = load_vault()
    cid = _id()
    meta = PROVIDERS.get(provider) or PROVIDERS["custom"]
    v[cid] = {
        "provider": provider,
        "label": label or meta["label"],
        "secret": secret,
        "env_var": env_var_for(provider, env_var),
        "scopes": scopes or [],
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "expires": expires,                    # ISO date string or None
        "last4": _last4(secret),
    }
    save_vault(v)
    return cid


# ── reference-backed credentials ─────────────────────────────────────────────
# Some records don't hold a stored value — they hold a *reference* to where a
# value already lives on this machine, resolved on demand so it always reflects
# the current state and is never duplicated into our store. `source` shapes:
#   {"kind": "cmd",  "argv": [...]}                 # value = stdout of a command
#   {"kind": "json", "path": "~/…", "keys": [...]}  # value at a nested json path
#   {"kind": "json_user", "path": "~/…"}            # first users{}.auth.token match
def add_source_cred(provider, label, env_var, source, scopes=None):
    """Register a reference-backed record (no value stored)."""
    v = load_vault()
    cid = _id()
    v[cid] = {
        "provider": provider,
        "label": label or provider,
        "source": source,
        "env_var": env_var_for(provider, env_var),
        "scopes": scopes or [],
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "expires": None,
        "last4": "live",
    }
    save_vault(v)
    return cid


def resolve_source(source):
    """Return the current value a reference points at, or None."""
    try:
        kind = source.get("kind")
        if kind == "cmd":
            r = subprocess.run(source["argv"], capture_output=True, text=True, timeout=15)
            return (r.stdout.strip() or None) if r.returncode == 0 else None
        if kind == "json":
            d = json.load(open(os.path.expanduser(source["path"])))
            for k in source.get("keys", []):
                d = d[k]
            return d or None
        if kind == "json_user":
            d = json.load(open(os.path.expanduser(source["path"])))
            for u in (d.get("users") or {}).values():
                t = ((u.get("auth") or {}).get("token")) or u.get("token")
                if t:
                    return t
            return None
    except Exception as e:
        print(f"  [vault] source resolve failed: {type(e).__name__}: {e}", flush=True)
    return None


def secret_of(cred):
    """The usable value for a record — resolved from its reference if it has one."""
    if not cred:
        return None
    if cred.get("source"):
        return resolve_source(cred["source"])
    return cred.get("secret")


def redact(cid, rec):
    """The only shape the browser ever sees — everything but the secret."""
    out = {k: rec.get(k) for k in
           ("provider", "label", "env_var", "scopes", "created", "expires", "last4")}
    out["id"] = cid
    out["live"] = bool(rec.get("source"))
    return out


def list_creds():
    return [redact(cid, rec) for cid, rec in load_vault().items()]


def get_cred(cid):
    return load_vault().get(cid)


def update_cred(cid, **fields):
    v = load_vault()
    if cid not in v:
        return False
    for k in ("label", "scopes", "expires", "env_var"):
        if k in fields and fields[k] is not None:
            v[cid][k] = fields[k]
    if fields.get("secret"):                   # rotation
        v[cid]["secret"] = fields["secret"]
        v[cid]["last4"] = _last4(fields["secret"])
    save_vault(v)
    return True


def delete_cred(cid):
    v = load_vault()
    if v.pop(cid, None) is None:
        return False
    save_vault(v)
    # Cascade: drop any grants that pointed at this credential.
    g = load_grants()
    g["grants"] = {gid: gr for gid, gr in g["grants"].items() if gr["cred_id"] != cid}
    save_grants(g)
    return True


# ── grants + requests ────────────────────────────────────────────────────────
def load_grants():
    try:
        d = json.loads(GRANTS_FILE.read_text())
        return {"requests": d.get("requests", {}), "grants": d.get("grants", {})}
    except Exception:
        return {"requests": {}, "grants": {}}


def save_grants(g):
    tmp = str(GRANTS_FILE) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps(g).encode())
    os.close(fd)
    os.replace(tmp, GRANTS_FILE)
    os.chmod(GRANTS_FILE, 0o600)


def add_request(uuid, service, reason=""):
    g = load_grants()
    # Coalesce: an identical still-pending ask from the same pane reuses its id
    # so a polling agent doesn't spam the owner with duplicates.
    now = time.time()
    for rid, r in g["requests"].items():
        if (r["status"] == "pending" and r["uuid"] == uuid
                and r["service"] == service and now - r["created"] < REQUEST_TTL):
            return rid
    rid = _id()
    g["requests"][rid] = {"uuid": uuid, "service": service,
                          "reason": (reason or "")[:200], "created": now,
                          "status": "pending"}
    save_grants(g)
    return rid


def pending_requests():
    now = time.time()
    return [{"id": rid, **r} for rid, r in load_grants()["requests"].items()
            if r["status"] == "pending" and now - r["created"] < REQUEST_TTL]


def pending_count():
    return len(pending_requests())


def get_request(rid):
    return load_grants()["requests"].get(rid)


def set_request_status(rid, status):
    g = load_grants()
    if rid in g["requests"]:
        g["requests"][rid]["status"] = status
        save_grants(g)


def add_grant(uuid, cred_id, scopes=None, expires=None):
    g = load_grants()
    gid = _id()
    g["grants"][gid] = {"uuid": uuid, "cred_id": cred_id, "scopes": scopes or [],
                        "granted_at": time.time(), "expires": expires}
    save_grants(g)
    return gid


def list_grants():
    """Grants joined with their credential's public shape — for the phone UI."""
    v = load_vault()
    out = []
    for gid, gr in load_grants()["grants"].items():
        cred = v.get(gr["cred_id"])
        if not cred:
            continue
        out.append({"id": gid, "uuid": gr["uuid"], "cred_id": gr["cred_id"],
                    "provider": cred["provider"], "label": cred["label"],
                    "env_var": cred["env_var"], "last4": cred["last4"],
                    "scopes": gr["scopes"], "granted_at": gr["granted_at"],
                    "expires": gr.get("expires")})
    return out


def revoke_grant(gid):
    g = load_grants()
    if g["grants"].pop(gid, None) is None:
        return False
    save_grants(g)
    return True


def _service_matches(service, cred):
    """Does an agent's `dispatch-auth get <service>` map to this credential?"""
    s = (service or "").lower()
    return s in (cred["provider"].lower(),
                 (cred.get("env_var") or "").lower(),
                 (cred.get("label") or "").lower())


def find_grant(uuid, service):
    """A live grant for (pane, service), or None. Skips expired grants/creds."""
    now = time.time()
    v = load_vault()
    for gid, gr in load_grants()["grants"].items():
        if gr["uuid"] != uuid:
            continue
        if gr.get("expires") and now > _epoch(gr["expires"]):
            continue
        cred = v.get(gr["cred_id"])
        if not cred:
            continue
        if cred.get("expires") and now > _epoch(cred["expires"]):
            continue
        if _service_matches(service, cred):
            return gid, gr, cred
    return None


def _epoch(when):
    """Best-effort parse of an expiry (epoch float or 'YYYY-MM-DD') → epoch."""
    if isinstance(when, (int, float)):
        return float(when)
    try:
        return time.mktime(time.strptime(str(when)[:10], "%Y-%m-%d"))
    except Exception:
        return float("inf")                    # unparseable → treat as non-expiring


def release(uuid, service):
    """Return (env_var, secret, cred_id, last4) if this pane may have `service`.

    The single choke point where a secret leaves the module. Callers audit the
    release with cred_id + last4 — never the secret itself.
    """
    m = find_grant(uuid, service)
    if not m:
        return None
    _gid, gr, cred = m
    secret = secret_of(cred)                    # resolves a live reference on demand
    if not secret:
        return None                             # reference went stale / tool logged out
    return cred["env_var"], secret, gr["cred_id"], _last4(secret)
