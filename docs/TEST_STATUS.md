# Test status (honest)

| What | Status | Date |
|---|---|---|
| 28 automated tests, mocked PayPal + mock AI | PASS 28/28 (Linux 3.11/3.13, Windows 11 Python 3.11.4) | 2026-11-09 |
| Mutation check of 4 safety guards | each break caught by ≥1 test | 2026-11-08 |
| Real Claude API: structured proposal | PASS (NS-SCAN-STD 9.00 EUR) | 2026-11-09 |
| Real Claude API: over-budget request (commercial licence 1497 EUR) | proposed, gate REJECT | 2026-11-09 |
| Real PayPal Sandbox: OAuth | PASS | 2026-11-09 |
| Real PayPal Sandbox: create order | PASS, order 8HE008159R316804W | 2026-11-09 |
| Real PayPal Sandbox: buyer approval (sandbox Personal account) | PASS, status APPROVED | 2026-11-09 |
| Real PayPal Sandbox: gate-checked capture | PASS, capture 2T479471T7932424S, COMPLETED, 9.00 EUR | 2026-11-09 |
| Duplicate capture / second order after capture | refused (409) | 2026-11-09 |
| Evidence export (JSON + CSV), chain re-verified independently | INTACT, 20 entries | 2026-11-09 |

Evidence: `docs/evidence/earlier_session_2026-11-09.json` and `.csv`. Sandbox only; no real money.
Gate used: public demo policy adapter (not the proprietary Proof-of-Silence engine).

## Demo video vs. evidence export (note, 2026-11-09)
The demo video and `docs/evidence/earlier_session_2026-11-09.json` come from the same local Sandbox session.
The export was taken earlier and contains the first 20 ledger entries (chain intact, head e79732c2…).
The video's ledger screens show the same session at a later state (up to 30 entries) after extra test proposals.
The video is edited from selected moments of that session; it is not one continuous recording.
The ledger is a local SHA-256 hash chain: tamper-evident, not independently signed.

## One-take Sandbox run (9 Nov 2026)
A second, fresh database was used for one continuous recorded run:
order `61G66411HL478774X`, capture `3L217610BM083854W`, 9.00 EUR COMPLETED, 11 ledger entries
(`docs/evidence/onetake_run_2026-11-09.*`). All 11 SHA-256 links were recomputed independently and
JSON and CSV agree row by row. Windows 11 / Python 3.11: 28/28 tests passed; `preflight --oauth --ai` READY.
