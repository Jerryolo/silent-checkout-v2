"""AI shopping agent: turns a natural-language request into a structured proposal.

The agent only receives the shopper's text and the public demo catalogue.
It never receives PayPal credentials, never calls PayPal and cannot approve anything.
Its output is untrusted: the server validates it and the PoS gate evaluates it.
"""
import json, os, re
import requests

CATALOG = [
    {"id": "NS-SCAN-STD", "name": "AI Risk Scanner (Standard)", "price": "9.00"},
    {"id": "NS-POS-STD", "name": "Proof-of-Silence Standard Edition", "price": "49.00"},
    {"id": "NS-POS-COM", "name": "Proof-of-Silence Commercial Licence", "price": "1497.00"},
]
SYSTEM = (
    "You are a shopping assistant for the neonai.shop sandbox demo. Choose exactly one item from the "
    "catalogue that best matches the shopper's request. Reply with JSON only, no prose: "
    '{"item_id": str, "amount": "0.00", "currency": "EUR", "reason": str}. '
    "Use the catalogue price exactly. Catalogue: " + json.dumps(CATALOG)
)


class AgentError(Exception):
    pass


def _parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise AgentError("model returned no JSON object")
    try:
        data = json.loads(m.group(0))
    except ValueError as exc:
        raise AgentError("model returned invalid JSON") from exc
    for k in ("item_id", "amount", "currency", "reason"):
        if k not in data:
            raise AgentError(f"model output missing '{k}'")
    return {"item_id": str(data["item_id"])[:40], "amount": str(data["amount"])[:20],
            "currency": str(data["currency"])[:3], "reason": str(data["reason"])[:300]}


def _anthropic(request_text):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise AgentError("ANTHROPIC_API_KEY not set")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": os.getenv("AI_MODEL", "claude-haiku-5-5"), "max_tokens": 300, "system": SYSTEM,
              "messages": [{"role": "user", "content": request_text[:500]}]},
        timeout=30,
    )
    if not r.ok:
        raise AgentError(f"AI API HTTP {r.status_code}")
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _mock(request_text):
    """Deterministic stand-in for tests and offline demos. Clearly labelled as MOCK in the UI."""
    t = request_text.lower()
    item = CATALOG[2] if ("commercial" in t or "resell" in t) else CATALOG[1] if ("proof" in t or "standard" in t) else CATALOG[0]
    return json.dumps({"item_id": item["id"], "amount": item["price"], "currency": "EUR",
                       "reason": f"MOCK agent picked {item['name']} by keyword match."})


def provider():
    return os.getenv("AI_PROVIDER", "anthropic").lower()


def propose(request_text):
    text = (request_text or "").strip()
    if not text:
        raise AgentError("empty request")
    raw = _mock(text) if provider() == "mock" else _anthropic(text)
    out = _parse(raw)
    out["provider"] = provider()
    return out
