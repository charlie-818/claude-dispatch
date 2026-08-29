#!/usr/bin/env python3
"""Manual prune of the vault — remove entries that aren't reusable API keys.

Four buckets, each an explicit hand-picked list (no heuristics — you can read
exactly what goes). Everything NOT listed here is kept.

  A. not-api-keys   wallet/contract addresses, private keys, app/session secrets,
                    OAuth client IDs, DSNs, sessions, placeholders, plain URLs
  B. public         NEXT_PUBLIC_* / VITE_* / REACT_APP_* — client-exposed, not secret
  C. databases      connection strings + DB/redis creds (project-specific)
  D. redundant      duplicate of a canonical entry we keep

Run:  cd /Users/charliebc/claude-dispatch && .venv/bin/python3 prune_vault.py
"""
import vault

NOT_API_KEYS = [
    "ADMIN_MULTISIG", "ADMIN_PASSWORD", "ALLOWED_WALLET_ADDRESS",
    "ANTHROPIC_LONG_TOKEN_IDS", "ANTHROPIC_SHORT_TOKEN_IDS", "API_KEYS",
    "AUTH_GOOGLE_ID", "AUTH_SECRET", "AUTH_URL", "BASE_SNAPSHOT_ID",
    "BONUS_SPONSOR_PRIVY_WALLET_ID", "BONUS_SPONSOR_WALLET_ADDRESS",
    "CACHE_REFRESH_SECRET", "CONTRACT_DEPLOYER_PRIVATE_KEY", "CRON_SECRET",
    "CTF_ADDRESS", "CTF_EXCHANGE_V2", "COLLATERAL_OFFRAMP", "COLLATERAL_ONRAMP",
    "DEPLOYER_ADDRESS", "DEPLOYER_PRIVATE_KEY", "EMAIL_PASSWORD", "ETH_PRIVATE_KEY",
    "EXECUTOR_ADDRESS", "EXECUTOR_PK", "FIREBASE_APP_CHECK_DEBUG_TOKEN",
    "GOOGLE_CLIENT_ID", "GOOGLE_DRIVE_FOLDER_ID", "GOOGLE_DRIVE_FOLDER_LANDING",
    "GOOGLE_DRIVE_FOLDER_SEARCH", "GOOGLE_DRIVE_FOLDER_SWAP", "INTERNAL_GRANT_SECRET",
    "JWT_SECRET", "KEEPER_PRIVATE_KEY", "MINTER_SIGNER_ADDRESS", "MINTER_SIGNER_PK",
    "NEG_RISK_ADAPTER", "NEG_RISK_EXCHANGE_V2", "NEXTAUTH_SECRET",
    "ORACLE_PUSHER_ADDRESS", "ORACLE_PUSHER_PK", "PAUSER_ADDRESS", "PAY_TO_ADDRESS",
    "PAY_TO_SOLANA", "PLATFORM_WALLET_ADDRESS", "POLYMARKET_PROXY_URL", "PRIVATE_KEY",
    "PRIVY_APP_ID", "PRIVY_AUTHORIZATION_KEY_ID", "PRIVY_AUTHORIZATION_KEY_IDS",
    "PRIVY_AUTHORIZATION_KEY_SECRETS", "PRIVY_AUTHORIZATION_PRIVATE_KEY",
    "PRIVY_AUTH_KEY", "PRIVY_TRADING_POLICY_ID", "PRIVY_WALLET_POLICY_ID",
    "PUSD_ADDRESS", "PYTH_BSC_MAINNET", "R2_ACCOUNT_ID", "RECAPTCHA_SITE_KEY",
    "REDDIT_PASSWORD", "SESSION_SECRET", "SHEET_SYNC_SECRET", "SOLANA_PRIVATE_KEY",
    "SSH_KEY", "STRIPE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY",
    "SUPABASE_PUBLISHABLE_KEY", "TELEGRAM_SESSION", "TOKENIZATION_EXECUTOR_PK",
    "TOKENIZATION_MINTER_SIGNER_PK", "TOKENIZATION_ORACLE_PUSHER_PK",
    "TOKENIZATION_PILOT_COMPANIES", "TREASURY_ADDRESS", "UNIV3_ROUTER",
    "USDC_ADDRESS", "USDC_E_ADDRESS", "VAULTO_USDC_TREASURY_ADDRESS",
    "VERCEL_OIDC_TOKEN", "WALLETCONNECT_PROJECT_ID",
    "YOUR_TREASURY_WALLET_ADDRESS_HERE", "polymarket_private__key",
    "SENTRY_DSN", "NEXT_PUBLIC_SENTRY_DSN", "X_PASSWORD", "GMAIL_APP_PASSWORD",
]

PUBLIC = [
    "NEXT_PUBLIC_ALPHA_VANTAGE_API_KEY", "NEXT_PUBLIC_DEMO_PRIVATE_KEY",
    "NEXT_PUBLIC_ETHERSCAN_API_KEY", "NEXT_PUBLIC_MARKETDATA_API_KEY",
    "NEXT_PUBLIC_MORALIS_API_KEY", "NEXT_PUBLIC_NETLIFY_SITE_ID",
    "NEXT_PUBLIC_NETLIFY_TOKEN", "NEXT_PUBLIC_PRIVY_APP_ID",
    "NEXT_PUBLIC_PROPY_CONTRACT", "NEXT_PUBLIC_REALT_CONTRACT",
    "NEXT_PUBLIC_STOCKDATA_ORG_API_TOKEN", "NEXT_PUBLIC_SUPABASE_ANON_KEY",
    "NEXT_PUBLIC_THE_GRAPH_API_KEY", "NEXT_PUBLIC_VAULTO_USDC_TREASURY_ADDRESS",
    "NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID", "VITE_ALPHA_VANTAGE_API_KEY",
    "VITE_ETHERSCAN_API_KEY", "REACT_APP_ETHERSCAN_API_KEY",
]

DATABASES = [
    "DATABASE_URL", "DIRECT_URL", "POSTGRES_URL", "POSTGRES_URL_NON_POOLING",
    "MONGO_PASSWORD", "KV_REST_API_TOKEN", "KV_REST_API_READ_ONLY_TOKEN",
]

# duplicate of a canonical we keep (canonical in the comment)
REDUNDANT = [
    "CLAUDE_API_KEY",              # -> ANTHROPIC_API_KEY
    "CANDIDATE_ANTHROPIC_API_KEY", # -> ANTHROPIC_API_KEY
    "AI_API_KEY",                  # -> PERPLEXITY_API_KEY
    "DEFAULT_OPENAI_API_KEY",      # -> OPENAI_API_KEY
    "AZURE_OPENAI_KEY",            # -> AZURE_OPENAI_API_KEY
    "INFURA_KEY",                  # -> INFURA_API_KEY
    "GROK_API_KEY",                # -> XAI_API_KEY
    "MARKET_DATA_API_TOKEN",       # -> MARKETDATA_API_KEY
    "MENTRA_API_KEY",              # -> MENTRAOS_API_KEY
    "VAULTO_API_TOKEN",            # -> VAULTO_API_KEY
    "DASHBOARD_API_KEYS",          # -> DASHBOARD_API_KEY
    "INFINITE_API_KEYS",           # blob list, no single canonical
    "COINBASE_API_KEY",            # -> COINBASE_API_KEY_ID
    "COINBASE_API_SECRET",         # -> COINBASE_API_KEY_SECRET
]

REMOVE = {}
for bucket, names in [("not-api-key", NOT_API_KEYS), ("public", PUBLIC),
                      ("database", DATABASES), ("redundant", REDUNDANT)]:
    for n in names:
        REMOVE[n] = bucket


def main():
    v = vault.load_vault()
    by_env = {}
    for cid, r in v.items():
        by_env.setdefault(r.get("env_var"), []).append(cid)

    removed, missing = [], []
    for env_var, bucket in REMOVE.items():
        cids = by_env.get(env_var)
        if not cids:
            missing.append(env_var)
            continue
        for cid in cids:
            if vault.delete_cred(cid):
                removed.append((bucket, env_var))

    print("REMOVED " + "─" * 60)
    for bucket in ("not-api-key", "public", "database", "redundant"):
        rows = [e for b, e in removed if b == bucket]
        print(f"\n  [{bucket}] {len(rows)}")
        for e in sorted(rows):
            print(f"    - {e}")
    if missing:
        print(f"\n  (not found, skipped: {len(missing)}) {', '.join(sorted(missing))}")

    kept = vault.list_creds()
    print("\n" + "═" * 68)
    print(f"KEPT — {len(kept)} real API keys/tokens")
    print("═" * 68)
    for c in sorted(kept, key=lambda c: c["env_var"]):
        print(f"  {c['provider']:<12} {c['env_var']}")
    print(f"\nRemoved {len(removed)}, kept {len(kept)}. Restart daemon to refresh UI.")


if __name__ == "__main__":
    main()
