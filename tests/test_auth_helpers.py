from aiohttp.test_utils import make_mocked_request

import auth


def _req(method="GET", path="/", headers=None, host=None):
    headers = headers or {}
    if host:
        headers.setdefault("Host", host)
    return make_mocked_request(method, path, headers=headers)


# ── client_ip ────────────────────────────────────────────────────────────────

def test_client_ip_uses_x_forwarded_for():
    req = _req(headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
    assert auth.client_ip(req) == "1.2.3.4"


def test_client_ip_falls_back_to_remote():
    req = _req()
    ip = auth.client_ip(req)
    assert ip in ("?", req.remote)


# ── is_secure ────────────────────────────────────────────────────────────────

def test_is_secure_https_scheme():
    req = make_mocked_request("GET", "/", headers={}, sslcontext=object())
    assert auth.is_secure(req) is True


def test_is_secure_forwarded_proto():
    req = _req(headers={"X-Forwarded-Proto": "https"})
    assert auth.is_secure(req) is True


def test_is_secure_false_plain_http():
    req = _req()
    assert auth.is_secure(req) is False


# ── rp_id ────────────────────────────────────────────────────────────────────

def test_rp_id_strips_port():
    req = _req(host="example.com:8788")
    assert auth.rp_id(req) == "example.com"


def test_rp_id_uses_forwarded_host():
    req = _req(headers={"X-Forwarded-Host": "phone.tailnet.ts.net:443"},
                host="127.0.0.1:8788")
    assert auth.rp_id(req) == "phone.tailnet.ts.net"


def test_rp_id_lowercases():
    req = _req(host="EXAMPLE.COM")
    assert auth.rp_id(req) == "example.com"


# ── origin ───────────────────────────────────────────────────────────────────

def test_origin_http():
    req = _req(host="127.0.0.1:8788")
    assert auth.origin(req) == "http://127.0.0.1:8788"


def test_origin_https_via_forwarded_proto():
    req = _req(headers={"X-Forwarded-Proto": "https"}, host="example.com")
    assert auth.origin(req) == "https://example.com"


# ── passkey_capable ──────────────────────────────────────────────────────────

def test_passkey_capable_bare_ip_false():
    req = _req(headers={"X-Forwarded-Proto": "https"}, host="127.0.0.1")
    assert auth.passkey_capable(req) is False


def test_passkey_capable_localhost_true_without_https():
    req = _req(host="localhost")
    assert auth.passkey_capable(req) is True


def test_passkey_capable_https_hostname_true():
    req = _req(headers={"X-Forwarded-Proto": "https"}, host="example.tailnet.ts.net")
    assert auth.passkey_capable(req) is True


def test_passkey_capable_false_plain_http_hostname():
    req = _req(host="example.tailnet.ts.net")
    assert auth.passkey_capable(req) is False


def test_passkey_capable_empty_host_false():
    req = _req(host="")
    assert auth.passkey_capable(req) is False


# ── same_origin ──────────────────────────────────────────────────────────────

def test_same_origin_get_always_true():
    req = _req(method="GET", host="example.com")
    assert auth.same_origin(req) is True


def test_same_origin_matching_origin_header():
    req = _req(method="POST", headers={"Origin": "http://example.com"},
                host="example.com")
    assert auth.same_origin(req) is True


def test_same_origin_mismatched_origin_header():
    req = _req(method="POST", headers={"Origin": "http://evil.com"},
                host="example.com")
    assert auth.same_origin(req) is False


def test_same_origin_no_header_no_sec_fetch_site_allowed():
    req = _req(method="POST", host="example.com")
    assert auth.same_origin(req) is True


def test_same_origin_no_header_cross_site_sec_fetch_rejected():
    req = _req(method="POST", headers={"Sec-Fetch-Site": "cross-site"},
                host="example.com")
    assert auth.same_origin(req) is False
