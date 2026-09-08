import time

import vault

# ── _last4 ───────────────────────────────────────────────────────────────────

def test_last4_normal():
    assert vault._last4("sk-ant-abcdef1234") == "1234"


def test_last4_short_string():
    assert vault._last4("abc") == "····"


def test_last4_not_a_string():
    assert vault._last4(None) == "····"
    assert vault._last4(12345) == "····"


# ── env_var_for ──────────────────────────────────────────────────────────────

def test_env_var_for_override_wins():
    assert vault.env_var_for("github", override="MY_VAR") == "MY_VAR"


def test_env_var_for_known_provider():
    assert vault.env_var_for("vercel") == "VERCEL_TOKEN"


def test_env_var_for_unknown_provider_falls_back_to_custom():
    expected = vault.PROVIDERS["custom"].get("env_var") or "API_TOKEN"
    assert vault.env_var_for("totally-unknown-provider") == expected


# ── infer_provider ───────────────────────────────────────────────────────────

def test_infer_provider_recognised_keyword():
    # infer_provider matches _SERVICES, a long-tail list distinct from the
    # main PROVIDERS registry (which doesn't include github/vercel).
    got = vault.infer_provider("MAPBOX_API_KEY")
    assert got is not None
    slug, label, domain = got
    assert slug == "mapbox"


def test_infer_provider_none_for_unrecognised():
    assert vault.infer_provider("SOME_RANDOM_VAR") is None


def test_infer_provider_empty():
    assert vault.infer_provider("") is None
    assert vault.infer_provider(None) is None


# ── guess_domain ─────────────────────────────────────────────────────────────

def test_guess_domain_known_provider():
    assert vault.guess_domain("GITHUB_TOKEN") == "github.com"


def test_guess_domain_heuristic_fallback():
    # "ACME_API_KEY" -> suffix stripped to "ACME" -> acme.com
    assert vault.guess_domain("ACME_API_KEY") == "acme.com"


def test_guess_domain_none_for_short_or_nonalpha_core():
    assert vault.guess_domain("A1_TOKEN") is None
    assert vault.guess_domain("") is None


# ── redact ───────────────────────────────────────────────────────────────────

def test_redact_strips_secret_and_adds_metadata():
    rec = {"provider": "github", "label": "GH", "env_var": "GITHUB_TOKEN",
           "scopes": [], "created": 1.0, "expires": None, "last4": "abcd",
           "secret": "super-secret-value", "source": None}
    out = vault.redact("cid1", rec)
    assert "secret" not in out
    assert out["id"] == "cid1"
    assert out["live"] is False
    assert out["domain"] == "github.com"


def test_redact_live_true_when_source_present():
    rec = {"provider": "custom", "label": "x", "env_var": "X_TOKEN",
           "scopes": [], "created": 1.0, "expires": None, "last4": "abcd",
           "source": {"kind": "cmd"}}
    out = vault.redact("cid2", rec)
    assert out["live"] is True


def test_redact_domain_uses_guess_when_provider_unknown():
    rec = {"provider": "custom", "label": "x", "env_var": "ACME_API_KEY",
           "scopes": [], "created": 1.0, "expires": None, "last4": "abcd"}
    out = vault.redact("cid3", rec)
    assert out["domain"] == "acme.com"


# ── _service_matches ─────────────────────────────────────────────────────────

def test_service_matches_provider():
    cred = {"provider": "GitHub", "env_var": "GITHUB_TOKEN", "label": "My GH"}
    assert vault._service_matches("github", cred)


def test_service_matches_env_var():
    cred = {"provider": "custom", "env_var": "ACME_TOKEN", "label": "Acme"}
    assert vault._service_matches("acme_token", cred)


def test_service_matches_label_case_insensitive():
    cred = {"provider": "custom", "env_var": "X", "label": "My Service"}
    assert vault._service_matches("MY SERVICE", cred)


def test_service_matches_false_when_nothing_matches():
    cred = {"provider": "github", "env_var": "GITHUB_TOKEN", "label": "GH"}
    assert not vault._service_matches("vercel", cred)


# ── _epoch ───────────────────────────────────────────────────────────────────

def test_epoch_numeric_passthrough():
    assert vault._epoch(1700000000) == 1700000000.0
    assert vault._epoch(1700000000.5) == 1700000000.5


def test_epoch_parses_date_string():
    result = vault._epoch("2024-01-15")
    expected = time.mktime(time.strptime("2024-01-15", "%Y-%m-%d"))
    assert result == expected


def test_epoch_unparseable_is_infinite():
    assert vault._epoch("not-a-date") == float("inf")
    assert vault._epoch(None) == float("inf")
