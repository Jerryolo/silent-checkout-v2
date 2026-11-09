# One controlled PayPal Sandbox test (Windows 11, Python 3.11)

New folder only (e.g. C:\Users\Jerzy\silent_checkout_v2_build). Never the old project, never port 5000.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q          # expect: 28 passed
Copy-Item .env.example .env                      # fill locally: PayPal Sandbox keys, AI key or AI_PROVIDER=mock
.\.venv\Scripts\python.exe preflight.py          # offline checks, expect READY
```

Only after Jerzy approves each step:
1. `preflight.py --oauth` — Sandbox token only, creates nothing.
2. `preflight.py --ai` — one AI request (small API cost).
3. Set `PAYPAL_ENABLE_CREATE=YES` (keep `PAYPAL_ENABLE_CAPTURE=NO`), start `app.py`, open http://127.0.0.1:5050,
   run ONE proposal → approve → "Create sandbox order". Expected: one Order ID, status PAYER_ACTION_REQUIRED.
   No money moves: capture is disabled.
4. Export JSON from the ledger as evidence.
