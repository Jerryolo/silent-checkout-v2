"""PayPal Orders v2, SANDBOX ONLY. Credentials are read here and nowhere else."""
import os
import requests

BASE = "https://api-m.sandbox.paypal.com"


class PayPalFailure(Exception):
    def __init__(self, details, definitely_no_side_effect=False):
        super().__init__(details.get("stage"))
        self.details = details
        self.safe = definitely_no_side_effect


def _err(resp, stage):
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return {"stage": stage, "http_status": resp.status_code, "debug_id": resp.headers.get("PayPal-Debug-Id"),
            "paypal_name": str(body.get("name", ""))[:100], "paypal_message": str(body.get("message", ""))[:250]}


def token():
    cid, secret = os.getenv("PAYPAL_CLIENT_ID"), os.getenv("PAYPAL_CLIENT_SECRET")
    if not cid or not secret:
        raise PayPalFailure({"stage": "config", "error": "missing sandbox credentials"}, True)
    try:
        r = requests.post(BASE + "/v1/oauth2/token", auth=(cid, secret),
                          data={"grant_type": "client_credentials"}, timeout=15)
    except requests.RequestException as exc:
        raise PayPalFailure({"stage": "oauth", "exception": type(exc).__name__}, True)
    if not r.ok:
        raise PayPalFailure(_err(r, "oauth"), True)  # no order can exist yet
    try:
        return r.json()["access_token"]
    except (ValueError, KeyError):
        raise PayPalFailure({"stage": "oauth", "error": "unexpected token response"}, True)


def create_order(pid, item_name, amount, return_url, cancel_url):
    tok = token()
    body = {"intent": "CAPTURE",
            "purchase_units": [{"reference_id": pid, "description": item_name[:120],
                                "amount": {"currency_code": "EUR", "value": amount}}],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": "neonai.shop", "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
                "return_url": return_url, "cancel_url": cancel_url}}}}
    try:
        r = requests.post(BASE + "/v2/checkout/orders", json=body, timeout=20,
                          headers={"Authorization": "Bearer " + tok, "PayPal-Request-Id": pid})
    except requests.RequestException as exc:
        raise PayPalFailure({"stage": "create_order", "exception": type(exc).__name__})
    if not r.ok:
        raise PayPalFailure(_err(r, "create_order"), r.status_code in (400, 401, 403, 422))
    try:
        data = r.json()
        oid = data["id"]
    except (ValueError, KeyError):
        raise PayPalFailure({"stage": "create_order", "error": "unexpected response, no order id"})
    link = next((l["href"] for l in data.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
    return {"order_id": oid, "approve_url": link, "status": data.get("status"),
            "debug_id": r.headers.get("PayPal-Debug-Id")}


def get_order(order_id):
    tok = token()
    try:
        r = requests.get(BASE + "/v2/checkout/orders/" + order_id, timeout=15,
                         headers={"Authorization": "Bearer " + tok})
    except requests.RequestException as exc:
        raise PayPalFailure({"stage": "get_order", "exception": type(exc).__name__}, True)
    if not r.ok:
        raise PayPalFailure(_err(r, "get_order"), True)
    try:
        d = r.json()
        amt = d["purchase_units"][0]["amount"]
        return {"order_id": d["id"], "status": d["status"], "amount": amt["value"], "currency": amt["currency_code"]}
    except (ValueError, KeyError, IndexError):
        raise PayPalFailure({"stage": "get_order", "error": "unexpected response"}, True)


def capture(order_id, request_id):
    tok = token()
    try:
        r = requests.post(BASE + f"/v2/checkout/orders/{order_id}/capture", json={}, timeout=20,
                          headers={"Authorization": "Bearer " + tok, "PayPal-Request-Id": request_id,
                                   "Content-Type": "application/json"})
    except requests.RequestException as exc:
        raise PayPalFailure({"stage": "capture", "exception": type(exc).__name__})
    if not r.ok:
        raise PayPalFailure(_err(r, "capture"))
    try:
        d = r.json()
        cap = d["purchase_units"][0]["payments"]["captures"][0]
        return {"order_status": d["status"], "capture_id": cap["id"], "capture_status": cap["status"],
                "amount": cap["amount"]["value"], "currency": cap["amount"]["currency_code"],
                "debug_id": r.headers.get("PayPal-Debug-Id")}
    except (ValueError, KeyError, IndexError):
        raise PayPalFailure({"stage": "capture", "error": "unexpected response"})
