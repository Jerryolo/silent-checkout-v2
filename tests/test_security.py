"""Security tests for Silent Checkout v2. All PayPal calls are mocked; no network, no money."""
import sqlite3, threading
import requests
from conftest import approved, propose, load_app


# 1. Missing human approval
def test_01_order_blocked_without_human_approval(env):
    _, c, fake, _ = env
    p, v = propose(c)
    assert v["decision"] == "ACT_NOW" and p["status"] == "PROPOSED"
    r = c.post(f"/api/paypal/create/{p['id']}")
    assert r.status_code == 409 and r.json["verdict"]["decision"] == "REJECT"
    assert fake.count("/v2/checkout/orders") == 0


# 2. Modified transaction amount
def test_02a_human_must_confirm_exact_amount(env):
    _, c, _, _ = env
    p, _ = propose(c)
    r = c.post(f"/api/approve/{p['id']}", json={"amount": "8.99", "item_id": p["item_id"]})
    assert r.status_code == 409
    assert c.get(f"/api/status/{p['id']}").json["proposal"]["status"] == "PROPOSED"


def test_02b_amount_changed_after_approval_is_rejected(env):
    m, c, fake, tmp = env
    p = approved(c)
    db = sqlite3.connect(tmp / "t.db")
    db.execute("UPDATE proposals SET amount='0.01' WHERE id=?", (p["id"],))
    db.commit(); db.close()
    r = c.post(f"/api/paypal/create/{p['id']}")
    assert r.status_code == 409 and "approval does not match" in " ".join(r.json["verdict"]["reasons"])
    assert fake.count("/v2/checkout/orders") == 0


def test_02c_buyer_amount_changed_in_paypal_blocks_capture(env):
    _, c, fake, _ = env
    p = approved(c)
    oid = c.post(f"/api/paypal/create/{p['id']}").json["order_id"]
    fake.buyer_approves(oid, amount="1.00")
    r = c.post(f"/api/paypal/capture/{p['id']}")
    assert r.status_code == 409 and r.json["verdict"]["decision"] == "REJECT"
    assert fake.count("/capture") == 0


# 3. Reused authorization
def test_03_approval_cannot_be_reused(env):
    _, c, fake, _ = env
    p = approved(c)
    assert c.post(f"/api/paypal/create/{p['id']}").status_code == 200
    assert c.post(f"/api/paypal/create/{p['id']}").status_code == 409
    assert c.post(f"/api/approve/{p['id']}", json={"amount": p["amount"], "item_id": p["item_id"]}).status_code == 409
    assert fake.count("/v2/checkout/orders") == 1


# 4. Duplicate requests
def test_04_duplicate_create_and_capture(env):
    _, c, fake, _ = env
    p = approved(c)
    oid = c.post(f"/api/paypal/create/{p['id']}").json["order_id"]
    for _ in range(3):
        assert c.post(f"/api/paypal/create/{p['id']}").status_code == 409
    fake.buyer_approves(oid)
    assert c.post(f"/api/paypal/capture/{p['id']}").status_code == 200
    assert c.post(f"/api/paypal/capture/{p['id']}").status_code == 409
    assert fake.count("/v2/checkout/orders") == 1 and fake.count("/capture") == 1


# 5. Concurrent requests
def test_05_concurrent_creates_produce_one_order(env):
    m, c, fake, _ = env
    p = approved(c)
    codes, start = [], threading.Barrier(10)

    def hit():
        start.wait()
        codes.append(m.app.test_client().post(f"/api/paypal/create/{p['id']}").status_code)
    ts = [threading.Thread(target=hit) for _ in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert codes.count(200) == 1 and codes.count(409) == 9
    assert fake.count("/v2/checkout/orders") == 1
    assert c.get("/api/evidence").json["verify"]["intact"]


# 6. Invalid credentials
def test_06_invalid_credentials_fail_safe(env):
    _, c, fake, _ = env
    fake.oauth_status = 401
    p = approved(c)
    r = c.post(f"/api/paypal/create/{p['id']}")
    assert r.status_code == 502 and r.json["status"] == "FAILED_SAFE"
    assert fake.count("/v2/checkout/orders") == 0
    body = str(c.get("/api/evidence").json)
    assert "test-secret" not in body and "tok" not in body.replace("token", "")


# 7. Network timeouts
def test_07a_timeout_on_create_is_frozen_never_retried(env):
    _, c, fake, _ = env
    fake.create_mode = "timeout"
    p = approved(c)
    r = c.post(f"/api/paypal/create/{p['id']}")
    assert r.status_code == 502 and r.json["status"] == "REVIEW_REQUIRED"
    fake.create_mode = "ok"
    assert c.post(f"/api/paypal/create/{p['id']}").status_code == 409
    assert fake.count("/v2/checkout/orders") == 1


def test_07b_timeout_on_capture_never_claims_captured(env):
    _, c, fake, _ = env
    p = approved(c)
    oid = c.post(f"/api/paypal/create/{p['id']}").json["order_id"]
    fake.buyer_approves(oid)
    fake.capture_mode = "timeout"
    assert c.post(f"/api/paypal/capture/{p['id']}").status_code == 502
    assert c.get(f"/api/status/{p['id']}").json["proposal"]["status"] == "REVIEW_REQUIRED"
    assert c.post(f"/api/paypal/capture/{p['id']}").status_code == 409


# 8. Unexpected PayPal responses
def test_08a_create_without_order_id_is_uncertain(env):
    _, c, fake, _ = env
    fake.create_mode = "no_id"
    p = approved(c)
    assert c.post(f"/api/paypal/create/{p['id']}").json["status"] == "REVIEW_REQUIRED"


def test_08b_pending_capture_is_not_reported_as_captured(env):
    _, c, fake, _ = env
    p = approved(c)
    oid = c.post(f"/api/paypal/create/{p['id']}").json["order_id"]
    fake.buyer_approves(oid)
    fake.capture_mode = "pending"
    r = c.post(f"/api/paypal/capture/{p['id']}")
    assert r.status_code == 502
    assert c.get(f"/api/status/{p['id']}").json["proposal"]["status"] == "REVIEW_REQUIRED"


# 9. Application restart
def test_09_restart_keeps_state_and_freezes_interrupted_calls(env):
    m, c, fake, tmp = env
    p1 = approved(c)
    c.post(f"/api/paypal/create/{p1['id']}")
    p2 = approved(c)
    db = sqlite3.connect(tmp / "t.db")
    db.execute("UPDATE proposals SET status='PROCESSING', approval_used=1 WHERE id=?", (p2["id"],))  # simulated crash mid-call
    db.commit(); db.close()
    m2 = load_app(tmp / "t.db")
    c2 = m2.app.test_client()
    assert c2.get(f"/api/status/{p1['id']}").json["proposal"]["status"] == "ORDER_CREATED"
    assert c2.get(f"/api/status/{p2['id']}").json["proposal"]["status"] == "REVIEW_REQUIRED"
    assert c2.post(f"/api/paypal/create/{p2['id']}").status_code == 409
    ev = c2.get("/api/evidence").json
    assert ev["verify"]["intact"] and any(e["event"] == "STARTUP_RECOVERY" for e in ev["entries"])


# 10. Database integrity
def test_10_ledger_detects_tampering(env):
    _, c, _, tmp = env
    approved(c)
    assert c.get("/api/evidence").json["verify"]["intact"]
    db = sqlite3.connect(tmp / "t.db")
    db.execute("UPDATE evidence SET payload=replace(payload,'9.00','0.01') WHERE event='HUMAN_APPROVE'")
    db.commit(); db.close()
    v = c.get("/api/evidence").json["verify"]
    assert not v["intact"]


# 11. Unauthorized payment capture
def test_11a_capture_before_buyer_approval_waits(env):
    _, c, fake, _ = env
    p = approved(c)
    c.post(f"/api/paypal/create/{p['id']}")
    r = c.post(f"/api/paypal/capture/{p['id']}")
    assert r.status_code == 409 and r.json["verdict"]["decision"] == "WAIT"
    assert fake.count("/capture") == 0


def test_11b_capture_without_any_order_is_refused(env):
    _, c, fake, _ = env
    p, _ = propose(c)
    assert c.post(f"/api/paypal/capture/{p['id']}").status_code == 409
    assert fake.count("/capture") == 0


def test_11c_capture_disabled_by_default(env, monkeypatch):
    _, c, fake, _ = env
    monkeypatch.setenv("PAYPAL_ENABLE_CAPTURE", "NO")
    p = approved(c)
    oid = c.post(f"/api/paypal/create/{p['id']}").json["order_id"]
    fake.buyer_approves(oid)
    assert c.post(f"/api/paypal/capture/{p['id']}").status_code == 403
    assert fake.count("/capture") == 0


def test_happy_path_end_to_end_mocked(env):
    _, c, fake, _ = env
    p = approved(c)
    o = c.post(f"/api/paypal/create/{p['id']}").json
    assert o["approve_url"].startswith("https://")
    fake.buyer_approves(o["order_id"])
    assert c.post(f"/api/paypal/check/{p['id']}").json["paypal"]["status"] == "APPROVED"
    r = c.post(f"/api/paypal/capture/{p['id']}")
    assert r.status_code == 200 and r.json["status"] == "CAPTURED"
    ev = c.get("/api/evidence").json
    assert ev["verify"]["intact"] and any(e["event"] == "CAPTURE_VERIFIED" for e in ev["entries"])


def test_05b_db_claim_holds_even_if_gate_says_yes(env, monkeypatch):
    """Defence in depth: a permissive (or compromised) gate still cannot cause a second order."""
    m, c, fake, _ = env
    import pos_gateway
    monkeypatch.setattr(m.GATE, "evaluate", lambda stage, ctx: pos_gateway.verdict("ACT_NOW", ["test: always yes"], "test"))
    p = approved(c)
    codes, start = [], threading.Barrier(10)

    def hit():
        start.wait()
        codes.append(m.app.test_client().post(f"/api/paypal/create/{p['id']}").status_code)
    ts = [threading.Thread(target=hit) for _ in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert fake.count("/v2/checkout/orders") == 1 and codes.count(200) == 1
