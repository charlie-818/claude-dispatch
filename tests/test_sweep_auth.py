import sweep_auth as sa

# ── looks_secret ─────────────────────────────────────────────────────────────

def test_looks_secret_known_fingerprint():
    assert sa.looks_secret("SOME_VAR", "sk-ant-" + "a" * 25)


def test_looks_secret_connection_string():
    assert sa.looks_secret("DB_URL", "postgres://user:pass@host:5432/db")


def test_looks_secret_false_for_empty_value():
    assert not sa.looks_secret("API_KEY", "")
    assert not sa.looks_secret("API_KEY", None)


def test_looks_secret_false_for_not_secret_name():
    assert not sa.looks_secret("NODE_ENV", "production")


def test_looks_secret_false_for_filesystem_path():
    assert not sa.looks_secret("SOME_TOKEN", "/usr/local/bin/foo")


def test_looks_secret_false_for_bare_url():
    assert not sa.looks_secret("SOME_TOKEN", "https://example.com")


def test_looks_secret_secrety_name_long_value():
    assert sa.looks_secret("MY_API_KEY", "abcdefgh12")


def test_looks_secret_secrety_name_short_value_rejected():
    assert not sa.looks_secret("MY_API_KEY", "short")


def test_looks_secret_secrety_name_value_with_space_rejected():
    assert not sa.looks_secret("MY_SECRET", "has a space here")


def test_looks_secret_unnamed_high_entropy_token():
    assert sa.looks_secret("RANDOM_VAR", "aB3dEf9hJk2LmN4pQr7sUvWx")


def test_looks_secret_unnamed_no_digit_rejected():
    assert not sa.looks_secret("RANDOM_VAR", "abcdefghijklmnopqrstuvwx")


def test_looks_secret_unnamed_too_short_rejected():
    assert not sa.looks_secret("RANDOM_VAR", "abc123")


# ── classify ─────────────────────────────────────────────────────────────────

def test_classify_by_signature():
    assert sa.classify("SOME_VAR", "sk-ant-" + "a" * 25) == "anthropic"


def test_classify_by_env2prov(monkeypatch):
    monkeypatch.setitem(sa.ENV2PROV, "MY_SPECIAL_TOKEN", "myprovider")
    assert sa.classify("MY_SPECIAL_TOKEN", "irrelevant-value") == "myprovider"


def test_classify_by_name_hint():
    # NAME_HINTS pairs (regex, provider) — use one guaranteed to exist via github
    got = sa.classify("GITHUB_TOKEN", "not-a-known-signature-value")
    assert got == "github"


def test_classify_falls_back_to_vault_infer(monkeypatch):
    import vault
    monkeypatch.setattr(vault, "infer_provider", lambda name: ("vercel", "Vercel", "vercel.com"))
    assert sa.classify("SOME_RANDOM_NAME", "value") == "vercel"


def test_classify_none_when_nothing_matches(monkeypatch):
    import vault
    monkeypatch.setattr(vault, "infer_provider", lambda name: None)
    assert sa.classify("TOTALLY_UNKNOWN_XYZ", "some-plain-value") is None


# ── parse_kv ─────────────────────────────────────────────────────────────────

def test_parse_kv_basic_export():
    text = 'export FOO=bar\nBAZ="quoted value"\n'
    result = dict(sa.parse_kv(text))
    assert result == {"FOO": "bar", "BAZ": "quoted value"}


def test_parse_kv_skips_comments_and_blanks():
    text = "# a comment\n\nFOO=bar\n"
    result = dict(sa.parse_kv(text))
    assert result == {"FOO": "bar"}


def test_parse_kv_single_quotes_stripped():
    text = "FOO='single quoted'\n"
    result = dict(sa.parse_kv(text))
    assert result == {"FOO": "single quoted"}


def test_parse_kv_ignores_non_kv_lines():
    text = "this is not a valid line\nFOO=bar\n"
    result = dict(sa.parse_kv(text))
    assert result == {"FOO": "bar"}


def test_parse_kv_empty_value():
    text = "FOO=\n"
    result = dict(sa.parse_kv(text))
    assert result == {"FOO": ""}
