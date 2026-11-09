"""Pre-flight check before the first controlled PayPal Sandbox test.

Default: offline only (no network, no PayPal, no AI calls). Never prints secret values.
  python preflight.py              -> config + local readiness
  python preflight.py --oauth      -> also requests a Sandbox OAuth token (no order, no money)
  python preflight.py --ai         -> also sends one test request to the AI model (small API cost)
Exit code 0 = READY, 1 = BLOCKED.
"""
import os, socket, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  - ' + detail) if detail else ''}")


def main(argv):
    from dotenv import load_dotenv
    env_file = ROOT / ".env"
    check(".env present", env_file.exists(), "copy .env.example to .env")
    load_dotenv(env_file)
    sys.path.insert(0, str(ROOT))
    check("Python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        import flask, requests  # noqa: F401
        check("dependencies installed", True)
    except ImportError as exc:
        check("dependencies installed", False, str(exc))
        return 1
    check("PAYPAL_CLIENT_ID set", bool(os.getenv("PAYPAL_CLIENT_ID")))
    check("PAYPAL_CLIENT_SECRET set", bool(os.getenv("PAYPAL_CLIENT_SECRET")))
    import paypal_client
    check("PayPal endpoint is Sandbox", paypal_client.BASE == "https://api-m.sandbox.paypal.com")
    check("capture disabled for first test", os.getenv("PAYPAL_ENABLE_CAPTURE", "NO") != "YES",
          "keep PAYPAL_ENABLE_CAPTURE=NO")
    prov = os.getenv("AI_PROVIDER", "anthropic")
    check("AI provider configured", prov == "mock" or bool(os.getenv("ANTHROPIC_API_KEY")), f"AI_PROVIDER={prov}")
    check("PoS gate mode valid", os.getenv("POS_MODE", "demo") in ("demo", "remote"), os.getenv("POS_MODE", "demo"))
    port = int(os.getenv("PORT", "5050"))
    check("PORT is 5050", port == 5050, str(port))
    with socket.socket() as s:
        free = s.connect_ex(("127.0.0.1", port)) != 0
    check(f"port {port} free", free, "stop whatever runs there (never the old app on 5000)")
    db = Path(os.getenv("CHECKOUT_DB", str(ROOT / "checkout.sqlite3")))
    if db.exists():
        c = sqlite3.connect(db)
        pending = c.execute("SELECT COUNT(*) FROM proposals WHERE status IN "
                            "('PROCESSING','CAPTURING','REVIEW_REQUIRED')").fetchone()[0]
        c.close()
        check("no unresolved v2 attempts", pending == 0, f"{pending} need manual review")
    if "--oauth" in argv:
        try:
            paypal_client.token()
            check("Sandbox OAuth token (no order created)", True)
        except paypal_client.PayPalFailure as exc:
            check("Sandbox OAuth token (no order created)", False, f"{exc.details.get('stage')} HTTP {exc.details.get('http_status')}")
    if "--ai" in argv:
        import ai_agent
        try:
            out = ai_agent.propose("I need a tool to check my AI project's EU AI Act exposure")
            check("AI model returns structured proposal", True, f"{out['item_id']} {out['amount']} {out['currency']}")
        except ai_agent.AgentError as exc:
            check("AI model returns structured proposal", False, str(exc))
    ok = all(r[1] for r in results)
    print("READY" if ok else "BLOCKED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
