import os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(tmp_path, **over):
    # Start from the real environment (Windows needs SYSTEMROOT etc.), then override.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PAYPAL_", "ANTHROPIC_", "AI_", "POS_"))}
    env.update({"CHECKOUT_DB": str(tmp_path / "x.db"), "PORT": "5050"}, **over)
    return subprocess.run([sys.executable, str(ROOT / "preflight.py")], capture_output=True, text=True, env=env, cwd=tmp_path)


def test_preflight_offline_blocks_without_keys(tmp_path):
    r = run(tmp_path, PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET="")
    assert "PayPal endpoint is Sandbox" in r.stdout and r.stdout.strip().endswith("BLOCKED")


def test_preflight_hides_secret_values(tmp_path):
    r = run(tmp_path, PAYPAL_CLIENT_ID="ID-VISIBLE-NEVER", PAYPAL_CLIENT_SECRET="SECRET-NEVER", AI_PROVIDER="mock")
    assert "SECRET-NEVER" not in r.stdout + r.stderr and "ID-VISIBLE-NEVER" not in r.stdout + r.stderr
