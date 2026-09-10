"""Wake-on-LAN helpers. Everything here is pure or filesystem-only, so it runs
on the Linux CI runners as well as macOS."""
import wake

# ── normalize_mac ────────────────────────────────────────────────────────────

def test_normalize_mac_pads_macos_short_octets():
    # macOS `arp` prints octets without leading zeros; unpadded, the magic packet
    # is built from the wrong bytes and wakes nothing.
    assert wake.normalize_mac("da:b9:f2:2f:3b:2") == "da:b9:f2:2f:3b:02"


def test_normalize_mac_accepts_dashes_and_uppercase():
    assert wake.normalize_mac("DA-B9-F2-2F-3B-02") == "da:b9:f2:2f:3b:02"


def test_normalize_mac_rejects_incomplete_arp_entry():
    assert wake.normalize_mac("(incomplete)") is None


def test_normalize_mac_rejects_junk():
    for bad in ("", None, "not-a-mac", "da:b9:f2:2f:3b", "da:b9:f2:2f:3b:02:11", "zz:b9:f2:2f:3b:02"):
        assert wake.normalize_mac(bad) is None


# ── is_randomized ────────────────────────────────────────────────────────────

def test_is_randomized_detects_private_wifi_address():
    # 0xda has the locally-administered bit (0x02) set.
    assert wake.is_randomized("da:b9:f2:2f:3b:02") is True


def test_is_randomized_false_for_burned_in_oui():
    assert wake.is_randomized("a4:83:e7:11:22:33") is False


# ── magic_packet ─────────────────────────────────────────────────────────────

def test_magic_packet_shape():
    pkt = wake.magic_packet("da:b9:f2:2f:3b:2")
    assert len(pkt) == 102                      # 6 sync bytes + 16 x 6-byte MAC
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("dab9f22f3b02")
    assert pkt[96:] == bytes.fromhex("dab9f22f3b02")


def test_magic_packet_rejects_bad_mac():
    try:
        wake.magic_packet("nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ── is_private_v4 ────────────────────────────────────────────────────────────

def test_is_private_v4():
    assert wake.is_private_v4("192.168.1.45") is True
    assert wake.is_private_v4("10.0.0.1") is True
    assert wake.is_private_v4("76.32.104.76") is False
    assert wake.is_private_v4("100.87.27.76") is False   # CGNAT / tailnet, not LAN
    assert wake.is_private_v4("garbage") is False


# ── the learned-target cache ─────────────────────────────────────────────────

def test_remember_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "TARGETS_FILE", tmp_path / "t.json")
    e = wake.remember("mac.example.ts.net", ip="192.168.1.45", mac="da:b9:f2:2f:3b:2")
    assert e["ip"] == "192.168.1.45"
    assert e["mac"] == "da:b9:f2:2f:3b:02"
    assert e["randomized"] is True
    assert wake.load()["mac.example.ts.net"]["mac"] == "da:b9:f2:2f:3b:02"


def test_remember_never_erases_a_known_mac(tmp_path, monkeypatch):
    # The whole point of the cache: a lookup that fails while the peer is asleep
    # must not destroy the MAC we learned while it was awake.
    monkeypatch.setattr(wake, "TARGETS_FILE", tmp_path / "t.json")
    wake.remember("mac.example.ts.net", ip="192.168.1.45", mac="da:b9:f2:2f:3b:02")
    monkeypatch.setattr(wake, "arp_mac", lambda ip: None)      # ARP entry has decayed
    e = wake.remember("mac.example.ts.net", ip="192.168.1.45")
    assert e["mac"] == "da:b9:f2:2f:3b:02"


def test_preflight_reports_missing_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "TARGETS_FILE", tmp_path / "t.json")
    monkeypatch.setattr(wake, "netmap_lan_ip", lambda h: None)
    p = wake.preflight("unknown.example.ts.net")
    assert p["ready"] is False
    assert "no MAC" in p["reason"]


def test_preflight_warns_about_rotating_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "TARGETS_FILE", tmp_path / "t.json")
    wake.remember("mac.example.ts.net", ip="192.168.1.45", mac="da:b9:f2:2f:3b:02")
    p = wake.preflight("mac.example.ts.net")
    assert p["ready"] is True
    assert "Private Wi-Fi Address" in p["warn"]


def test_wake_refuses_without_a_mac(tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "TARGETS_FILE", tmp_path / "t.json")
    monkeypatch.setattr(wake, "netmap_lan_ip", lambda h: None)
    r = wake.wake("unknown.example.ts.net")
    assert r["ok"] is False and r["sent"] == 0 and r["woke"] is False


def test_wake_reports_no_false_success(tmp_path, monkeypatch):
    # Packets sent but nobody answered: ok (we sent it) yet woke=False with a reason.
    monkeypatch.setattr(wake, "TARGETS_FILE", tmp_path / "t.json")
    wake.remember("mac.example.ts.net", ip="192.168.1.45", mac="a4:83:e7:11:22:33")
    monkeypatch.setattr(wake, "send_magic", lambda mac, ip=None, repeat=3: 12)
    monkeypatch.setattr(wake, "is_up", lambda ip, timeout=1.5: False)
    r = wake.wake("mac.example.ts.net", wait=1)
    assert r["ok"] is True and r["woke"] is False and "did not answer" in r["reason"]
