import importlib.util, sys, threading
from pathlib import Path
import pytest, requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {"PayPal-Debug-Id": "dbg-test"}
        self.ok = 200 <= status < 300

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakePayPal:
    """In-memory stand-in for the PayPal Sandbox REST API. No network."""
    def __init__(self):
        self.calls, self.orders, self.lock = [], {}, threading.Lock()
        self.oauth_status, self.create_mode, self.capture_mode = 200, "ok", "ok"

    def post(self, url, **kw):
        with self.lock:
            self.calls.append(("POST", url))
        if url.endswith("/v1/oauth2/token"):
            return Resp(self.oauth_status, {"access_token": "tok"} if self.oauth_status == 200 else {"name": "invalid_client"})
        if url.endswith("/v2/checkout/orders"):
            if self.create_mode == "timeout":
                raise requests.Timeout("simulated")
            if self.create_mode == "no_id":
                return Resp(201, {"status": "CREATED"})
            with self.lock:
                oid = f"ORDER{len(self.orders) + 1}"
                amt = kw["json"]["purchase_units"][0]["amount"]
                self.orders[oid] = {"status": "PAYER_ACTION_REQUIRED", "amount": dict(amt)}
            return Resp(201, {"id": oid, "status": "PAYER_ACTION_REQUIRED",
                              "links": [{"rel": "payer-action", "href": f"https://sandbox.paypal.test/{oid}"}]})
        if url.endswith("/capture"):
            oid = url.split("/")[-2]
            if self.capture_mode == "timeout":
                raise requests.Timeout("simulated")
            o = self.orders[oid]
            st = "PENDING" if self.capture_mode == "pending" else "COMPLETED"
            o["status"] = "COMPLETED"
            return Resp(201, {"id": oid, "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
                {"id": "CAP-" + oid, "status": st, "amount": o["amount"]}]}}]})
        return Resp(404, {"name": "NOT_FOUND"})

    def get(self, url, **kw):
        with self.lock:
            self.calls.append(("GET", url))
        oid = url.split("/")[-1]
        o = self.orders.get(oid)
        if not o:
            return Resp(404, {"name": "RESOURCE_NOT_FOUND"})
        return Resp(200, {"id": oid, "status": o["status"], "purchase_units": [{"amount": o["amount"]}]})

    def count(self, suffix):
        return sum(1 for m, u in self.calls if u.endswith(suffix))

    def buyer_approves(self, oid, amount=None):
        self.orders[oid]["status"] = "APPROVED"
        if amount:
            self.orders[oid]["amount"]["value"] = amount


def load_app(db):
    spec = importlib.util.spec_from_file_location("checkout_app_" + str(abs(hash(str(db)))) , ROOT / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKOUT_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("AI_PROVIDER", "mock")
    monkeypatch.setenv("POS_MODE", "demo")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("PAYPAL_ENABLE_CREATE", "YES")
    monkeypatch.setenv("PAYPAL_ENABLE_CAPTURE", "YES")
    fake = FakePayPal()
    import paypal_client
    monkeypatch.setattr(paypal_client.requests, "post", fake.post)
    monkeypatch.setattr(paypal_client.requests, "get", fake.get)
    mod = load_app(tmp_path / "t.db")
    return mod, mod.app.test_client(), fake, tmp_path


def propose(client, text="I need an AI risk check"):
    r = client.post("/api/propose", json={"request": text})
    return r.json["proposal"], r.json["verdict"]


def approved(client, text="I need an AI risk check"):
    p, _ = propose(client, text)
    assert client.post(f"/api/approve/{p['id']}", json={"amount": p["amount"], "item_id": p["item_id"]}).status_code == 200
    return p
