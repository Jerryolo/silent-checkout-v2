"""Proof-of-Silence(TM) gateway interface.

Every consequential step asks the gateway for a verdict before it happens:
  PROPOSE       - may this AI proposal be shown to the human?
  CREATE_ORDER  - may a PayPal order be created for it?
  CAPTURE       - may the approved PayPal order be captured?

Verdicts: ACT_NOW (proceed), WAIT (not yet), REJECT (never for this request).
Anything unexpected is treated as REJECT (fail-closed).

Two implementations:
  DemoPolicyAdapter  - PUBLIC, OPEN SOURCE. A small, readable rule set written for this
                       hackathon demo. It is NOT the proprietary Proof-of-Silence engine.
  RemoteGateway      - client for a private Proof-of-Silence gateway over HTTP
                       (POS_GATEWAY_URL). The private engine is not part of this repository.
Select with POS_MODE=demo (default) or POS_MODE=remote.
"""
import os, time
from decimal import Decimal
import requests

ACT_NOW, WAIT, REJECT = "ACT_NOW", "WAIT", "REJECT"


def verdict(decision, reasons, gate):
    return {"decision": decision, "reasons": list(reasons), "gate": gate}


class DemoPolicyAdapter:
    name = "demo-policy-adapter (public, not the proprietary PoS engine)"

    def __init__(self, catalog, budget_eur=None, approval_ttl_s=None):
        self.catalog = {c["id"]: c for c in catalog}
        self.budget = Decimal(str(budget_eur or os.getenv("DEMO_BUDGET_EUR", "100.00")))
        self.ttl = int(approval_ttl_s or os.getenv("APPROVAL_TTL_SECONDS", "900"))

    def evaluate(self, stage, ctx):
        fn = {"PROPOSE": self._propose, "CREATE_ORDER": self._create, "CAPTURE": self._capture}.get(stage)
        if not fn:
            return verdict(REJECT, [f"unknown stage {stage}"], self.name)
        try:
            return fn(ctx)
        except Exception as exc:  # fail-closed
            return verdict(REJECT, [f"gate error: {type(exc).__name__}"], self.name)

    def _propose(self, c):
        r = []
        item = self.catalog.get(c["item_id"])
        if not item:
            r.append("item not in catalogue")
        elif Decimal(c["amount"]) != Decimal(item["price"]):
            r.append("amount differs from catalogue price")
        if c["currency"] != "EUR":
            r.append("currency must be EUR")
        if item and Decimal(c["amount"]) > self.budget:
            r.append(f"amount exceeds owner budget {self.budget} EUR")
        return verdict(REJECT, r, self.name) if r else verdict(ACT_NOW, ["proposal within policy"], self.name)

    def _create(self, c):
        r = []
        if not c.get("approved_at"):
            return verdict(REJECT, ["no human approval"], self.name)
        if c.get("approval_binding") != c.get("expected_binding"):
            r.append("approval does not match current item/amount")
        if c.get("approval_used"):
            r.append("approval already used")
        if r:
            return verdict(REJECT, r, self.name)
        if time.time() - c["approved_at"] > self.ttl:
            return verdict(REJECT, ["approval expired"], self.name)
        return verdict(ACT_NOW, ["human approval valid and unused"], self.name)

    def _capture(self, c):
        if not c.get("approved_at") or not c.get("order_id"):
            return verdict(REJECT, ["no approved proposal with an order"], self.name)
        if c.get("captured"):
            return verdict(REJECT, ["already captured"], self.name)
        st = c.get("paypal_status")
        if st in ("CREATED", "PAYER_ACTION_REQUIRED", "SAVED"):
            return verdict(WAIT, ["buyer has not approved in PayPal yet"], self.name)
        if st != "APPROVED":
            return verdict(REJECT, [f"unexpected PayPal status {st}"], self.name)
        if c.get("paypal_order_id") != c["order_id"]:
            return verdict(REJECT, ["PayPal order id mismatch"], self.name)
        if (c.get("paypal_amount"), c.get("paypal_currency")) != (c["amount"], "EUR"):
            return verdict(REJECT, ["PayPal amount differs from human-approved amount"], self.name)
        return verdict(ACT_NOW, ["buyer approved exact amount in PayPal"], self.name)


class RemoteGateway:
    """Client for a private Proof-of-Silence gateway. Contract (JSON over HTTPS):
    POST {POS_GATEWAY_URL}/v1/evaluate  {"stage": str, "context": {...}}
    -> 200 {"decision": "ACT_NOW"|"WAIT"|"REJECT", "reasons": [str]}
    Any other response, timeout or error -> REJECT (fail-closed)."""
    name = "remote-pos-gateway"

    def __init__(self, url=None, token=None):
        self.url = (url or os.getenv("POS_GATEWAY_URL", "")).rstrip("/")
        self.token = token or os.getenv("POS_GATEWAY_TOKEN", "")

    def evaluate(self, stage, ctx):
        if not self.url:
            return verdict(REJECT, ["POS_GATEWAY_URL not configured"], self.name)
        try:
            r = requests.post(self.url + "/v1/evaluate", json={"stage": stage, "context": ctx},
                              headers={"Authorization": "Bearer " + self.token} if self.token else {}, timeout=5)
            data = r.json() if r.ok else {}
            d = data.get("decision")
            if d not in (ACT_NOW, WAIT, REJECT):
                return verdict(REJECT, ["gateway returned no valid decision"], self.name)
            return verdict(d, [str(x)[:200] for x in data.get("reasons", [])][:10], self.name)
        except (requests.RequestException, ValueError):
            return verdict(REJECT, ["gateway unreachable"], self.name)


def build(catalog):
    return RemoteGateway() if os.getenv("POS_MODE", "demo") == "remote" else DemoPolicyAdapter(catalog)
