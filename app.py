"""neonai.shop - Silent Checkout v2 (PayPal AI Hackathon 2026).

Flow: shopper request -> AI proposal -> PoS gate -> human approves exact amount
      -> PayPal Sandbox order -> buyer approves in PayPal -> status check -> PoS gate
      -> capture (only if enabled) -> verified outcome in the evidence ledger.
"""
import csv, hashlib, io, json, os, sqlite3, time, uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, Response

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
import ai_agent, ledger, paypal_client, pos_gateway  # noqa: E402

app = Flask(__name__)
DB = Path(os.getenv("CHECKOUT_DB", str(ROOT / "checkout.sqlite3")))
GATE = pos_gateway.build(ai_agent.CATALOG)
CATALOG = {c["id"]: c for c in ai_agent.CATALOG}
FIELDS = ("id", "request_text", "item_id", "item_name", "amount", "currency", "ai_reason", "ai_provider",
          "status", "approved_at", "approval_used", "order_id", "paypal_status", "capture_id", "created_at")


def connect():
    c = sqlite3.connect(DB, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    c.execute("PRAGMA journal_mode=WAL")
    return c


@contextmanager
def tx():
    """Serialised write transaction: proposal state and evidence change together or not at all."""
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def init_db():
    with tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, request_text TEXT NOT NULL,"
                  " item_id TEXT NOT NULL, item_name TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,"
                  " ai_reason TEXT, ai_provider TEXT, status TEXT NOT NULL, approval_binding TEXT,"
                  " approved_at REAL, approval_used INTEGER NOT NULL DEFAULT 0, order_id TEXT UNIQUE,"
                  " paypal_status TEXT, capture_id TEXT, created_at INTEGER NOT NULL)")
        ledger.init(c)
        # Crash recovery: anything interrupted mid-call to PayPal is uncertain. Freeze it, never retry.
        for r in c.execute("SELECT id,status FROM proposals WHERE status IN ('PROCESSING','CAPTURING')").fetchall():
            c.execute("UPDATE proposals SET status='REVIEW_REQUIRED' WHERE id=?", (r["id"],))
            ledger.append(c, "STARTUP_RECOVERY", {"id": r["id"], "was": r["status"], "now": "REVIEW_REQUIRED"})


def money(v):
    try:
        d = Decimal(str(v))
        if not d.is_finite() or d <= 0 or d.as_tuple().exponent < -2:
            return None
        return f"{d:.2f}"
    except (InvalidOperation, ValueError):
        return None


def binding(p):
    return hashlib.sha256(f"{p['id']}|{p['item_id']}|{p['amount']}|{p['currency']}".encode()).hexdigest()


def row(c, pid):
    return c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()


def public(p):
    return {k: p[k] for k in FIELDS}


def gate_ctx(p, **extra):
    ctx = {"id": p["id"], "item_id": p["item_id"], "amount": p["amount"], "currency": p["currency"],
           "approved_at": p["approved_at"], "approval_used": bool(p["approval_used"]),
           "approval_binding": p["approval_binding"], "expected_binding": binding(p),
           "order_id": p["order_id"], "captured": p["status"] == "CAPTURED"}
    ctx.update(extra)
    return ctx


def enabled(flag):
    return os.getenv(flag) == "YES"


@app.get("/")
def home():
    return render_template("index.html", ai_provider=ai_agent.provider(), gate=GATE.name,
                           create_enabled=enabled("PAYPAL_ENABLE_CREATE"), capture_enabled=enabled("PAYPAL_ENABLE_CAPTURE"))


@app.post("/api/propose")
def propose():
    text = str((request.get_json(silent=True) or {}).get("request", "")).strip()[:500]
    if not text:
        return jsonify(error="Describe what you want to buy."), 400
    try:
        out = ai_agent.propose(text)
    except ai_agent.AgentError as exc:
        with tx() as c:
            ledger.append(c, "AI_ERROR", {"error": str(exc)})
        return jsonify(error=f"AI agent failed: {exc}"), 502
    amt = money(out["amount"])
    item = CATALOG.get(out["item_id"])
    pid = str(uuid.uuid4())
    p = {"id": pid, "item_id": out["item_id"], "amount": amt or out["amount"], "currency": out["currency"]}
    v = GATE.evaluate("PROPOSE", {**p, "amount": amt or "0"}) if amt else \
        pos_gateway.verdict(pos_gateway.REJECT, ["AI returned an invalid amount"], GATE.name)
    status = "PROPOSED" if v["decision"] == pos_gateway.ACT_NOW else "BLOCKED"
    with tx() as c:
        c.execute("INSERT INTO proposals(id,request_text,item_id,item_name,amount,currency,ai_reason,ai_provider,"
                  "status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (pid, text, out["item_id"], item["name"] if item else "UNKNOWN ITEM", p["amount"], out["currency"],
                   out["reason"], out["provider"], status, int(time.time())))
        ledger.append(c, "AI_PROPOSAL", {"id": pid, "item_id": out["item_id"], "amount": p["amount"],
                                         "currency": out["currency"], "provider": out["provider"]})
        ledger.append(c, "GATE_PROPOSE", {"id": pid, **v})
        prop = public(row(c, pid))
    return jsonify(proposal=prop, verdict=v), 201


@app.post("/api/approve/<pid>")
def approve(pid):
    body = request.get_json(silent=True) or {}
    with tx() as c:
        p = row(c, pid)
        if not p or p["status"] != "PROPOSED":
            return jsonify(error="Proposal missing or not awaiting approval."), 409
        if money(body.get("amount")) != p["amount"] or body.get("item_id") != p["item_id"]:
            ledger.append(c, "HUMAN_APPROVAL_MISMATCH", {"id": pid, "shown": p["amount"], "confirmed": str(body.get("amount"))[:20]})
            return jsonify(error="Approval must confirm the exact item and amount shown."), 409
        c.execute("UPDATE proposals SET status='APPROVED', approved_at=?, approval_binding=? WHERE id=? AND status='PROPOSED'",
                  (time.time(), binding(p), pid))
        ledger.append(c, "HUMAN_APPROVE", {"id": pid, "item_id": p["item_id"], "amount": p["amount"], "binding": binding(p)})
        return jsonify(proposal=public(row(c, pid)))


@app.post("/api/reject/<pid>")
def reject(pid):
    with tx() as c:
        cur = c.execute("UPDATE proposals SET status='REJECTED' WHERE id=? AND status='PROPOSED'", (pid,))
        if cur.rowcount != 1:
            return jsonify(error="Proposal missing or not rejectable."), 409
        ledger.append(c, "HUMAN_REJECT", {"id": pid})
    return jsonify(status="REJECTED")


@app.get("/api/status/<pid>")
def status(pid):
    c = connect()
    try:
        p = row(c, pid)
    finally:
        c.close()
    return (jsonify(proposal=public(p)), 200) if p else (jsonify(error="Not found"), 404)


@app.post("/api/paypal/create/<pid>")
def create_order(pid):
    if not enabled("PAYPAL_ENABLE_CREATE"):
        return jsonify(error="PayPal order creation is disabled (PAYPAL_ENABLE_CREATE=NO)."), 403
    with tx() as c:
        p = row(c, pid)
        if not p:
            return jsonify(error="Not found"), 404
        v = GATE.evaluate("CREATE_ORDER", gate_ctx(p))
        ledger.append(c, "GATE_CREATE_ORDER", {"id": pid, **v})
        if v["decision"] != pos_gateway.ACT_NOW:
            return jsonify(error="Blocked by Proof-of-Silence gate.", verdict=v), 409
        # Atomic one-shot claim: only one request can move APPROVED -> PROCESSING.
        cur = c.execute("UPDATE proposals SET status='PROCESSING', approval_used=1 WHERE id=? AND status='APPROVED' AND approval_used=0", (pid,))
        if cur.rowcount != 1:
            return jsonify(error="Already attempted. Never retried automatically."), 409
        ledger.append(c, "ORDER_ATTEMPT", {"id": pid})
        p = row(c, pid)
    base = request.host_url.rstrip("/")
    try:
        o = paypal_client.create_order(pid, p["item_name"], p["amount"], f"{base}/paypal/return", f"{base}/paypal/cancel")
    except paypal_client.PayPalFailure as exc:
        new = "FAILED_SAFE" if exc.safe else "REVIEW_REQUIRED"
        with tx() as c:
            c.execute("UPDATE proposals SET status=? WHERE id=? AND status='PROCESSING'", (new, pid))
            ledger.append(c, "ORDER_FAILED_SAFE" if exc.safe else "ORDER_UNCERTAIN", {"id": pid, **exc.details})
        msg = "PayPal rejected the request; no order exists." if exc.safe else "Outcome uncertain; manual review required. No retry."
        return jsonify(error=msg, status=new, diagnostic=exc.details), 502
    with tx() as c:
        c.execute("UPDATE proposals SET status='ORDER_CREATED', order_id=?, paypal_status=? WHERE id=? AND status='PROCESSING'",
                  (o["order_id"], o["status"], pid))
        ledger.append(c, "ORDER_CREATED", {"id": pid, "order_id": o["order_id"], "paypal_status": o["status"], "debug_id": o["debug_id"]})
    return jsonify(order_id=o["order_id"], approve_url=o["approve_url"], status="ORDER_CREATED")


@app.get("/paypal/return")
@app.get("/paypal/cancel")
def paypal_back():
    oid = request.args.get("token", "")[:64]
    c = connect()
    try:
        p = c.execute("SELECT id FROM proposals WHERE order_id=?", (oid,)).fetchone()
    finally:
        c.close()
    return redirect("/?pid=" + p["id"] if p else "/")


@app.post("/api/paypal/check/<pid>")
def check(pid):
    """Read-only: ask PayPal for the order's real status. Safe to repeat; never creates or captures."""
    c = connect()
    try:
        p = row(c, pid)
    finally:
        c.close()
    if not p or not p["order_id"]:
        return jsonify(error="No PayPal order for this proposal."), 409
    try:
        o = paypal_client.get_order(p["order_id"])
    except paypal_client.PayPalFailure as exc:
        with tx() as c:
            ledger.append(c, "STATUS_CHECK_FAILED", {"id": pid, **exc.details})
        return jsonify(error="Could not read PayPal status.", diagnostic=exc.details), 502
    with tx() as c:
        c.execute("UPDATE proposals SET paypal_status=? WHERE id=?", (o["status"], pid))
        ledger.append(c, "PAYPAL_STATUS", {"id": pid, **o})
    return jsonify(paypal=o)


@app.post("/api/paypal/capture/<pid>")
def capture(pid):
    if not enabled("PAYPAL_ENABLE_CAPTURE"):
        return jsonify(error="Capture is disabled (PAYPAL_ENABLE_CAPTURE=NO)."), 403
    c = connect()
    try:
        p = row(c, pid)
    finally:
        c.close()
    if not p or p["status"] != "ORDER_CREATED":
        return jsonify(error="Nothing capturable for this proposal."), 409
    try:
        o = paypal_client.get_order(p["order_id"])
    except paypal_client.PayPalFailure as exc:
        return jsonify(error="Could not read PayPal status; nothing captured.", diagnostic=exc.details), 502
    with tx() as c:
        p = row(c, pid)
        v = GATE.evaluate("CAPTURE", gate_ctx(p, paypal_status=o["status"], paypal_order_id=o["order_id"],
                                              paypal_amount=o["amount"], paypal_currency=o["currency"]))
        ledger.append(c, "GATE_CAPTURE", {"id": pid, "paypal_status": o["status"], **v})
        if v["decision"] != pos_gateway.ACT_NOW:
            return jsonify(error="Blocked by Proof-of-Silence gate.", verdict=v), 409
        cur = c.execute("UPDATE proposals SET status='CAPTURING' WHERE id=? AND status='ORDER_CREATED'", (pid,))
        if cur.rowcount != 1:
            return jsonify(error="Capture already in progress."), 409
        ledger.append(c, "CAPTURE_ATTEMPT", {"id": pid, "order_id": p["order_id"]})
    try:
        r = paypal_client.capture(p["order_id"], pid + ":capture")
    except paypal_client.PayPalFailure as exc:
        with tx() as c:
            c.execute("UPDATE proposals SET status='REVIEW_REQUIRED' WHERE id=? AND status='CAPTURING'", (pid,))
            ledger.append(c, "CAPTURE_UNCERTAIN", {"id": pid, **exc.details})
        return jsonify(error="Capture outcome uncertain; manual review required. No retry.", diagnostic=exc.details), 502
    ok = (r["capture_status"] == "COMPLETED" and r["order_status"] == "COMPLETED"
          and (r["amount"], r["currency"]) == (p["amount"], "EUR"))
    with tx() as c:
        c.execute("UPDATE proposals SET status=?, capture_id=?, paypal_status=? WHERE id=? AND status='CAPTURING'",
                  ("CAPTURED" if ok else "REVIEW_REQUIRED", r["capture_id"], r["order_status"], pid))
        ledger.append(c, "CAPTURE_VERIFIED" if ok else "CAPTURE_UNVERIFIED", {"id": pid, **r})
    return (jsonify(status="CAPTURED", capture=r), 200) if ok else \
        (jsonify(error="PayPal did not confirm a completed capture of the approved amount.", capture=r), 502)


@app.get("/api/evidence")
def evidence():
    c = connect()
    try:
        es, v = ledger.entries(c), ledger.verify(c)
    finally:
        c.close()
    return jsonify(entries=es, verify=v, gate=GATE.name,
                   note="Local SHA-256 chain in SQLite; detects edits, deletions and reordering. Not externally anchored.")


@app.get("/api/export.json")
def export_json():
    c = connect()
    try:
        props = [public(r) for r in c.execute("SELECT * FROM proposals ORDER BY created_at").fetchall()]
        es, v = ledger.entries(c), ledger.verify(c)
    finally:
        c.close()
    body = {"tool": "neonai.shop Silent Checkout v2", "exported_at": int(time.time()), "gate": GATE.name,
            "ai_provider": ai_agent.provider(), "proposals": props, "evidence": es, "verify": v}
    return Response(json.dumps(body, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=silent_checkout_evidence.json"})


@app.get("/api/export.csv")
def export_csv():
    c = connect()
    try:
        es = ledger.entries(c)
    finally:
        c.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["seq", "time_ms", "event", "proposal_id", "decision", "payload_json", "previous", "hash"])
    for e in es:
        w.writerow([e["seq"], e["time"], e["event"], e["payload"].get("id", ""), e["payload"].get("decision", ""),
                    ledger.canon(e["payload"]), e["previous"], e["hash"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=silent_checkout_evidence.csv"})


init_db()
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5050")), debug=False)
