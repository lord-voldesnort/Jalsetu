# JalSetu AI

**Transparent Water Request & Allocation Management**

JalSetu AI connects farmers’ water requests with accountable, explainable allocation decisions and a shared request history. One request. One accountable decision. One shared history.

This repository’s current product scope is the final MVP plan in `plan.md` and `TODO.md`; the former field-map/forecast prototype is no longer the primary experience. Legacy APIs remain temporarily for compatibility. Previous product documents and the field-map UI are preserved under `archive/field-monitoring-v1/` and are not part of the active MVP.

## What the demo does

- Two roles: Farmer and Water Manager, with server-checked ownership and service-area access.
- English and Marathi interface languages, switchable on the sign-in and dashboard screens; the choice is saved in the browser.
- Farmer requests in whole cubic metres, with purpose, required dates within one open supply period, reason, optional self-declared area/crop, and duplicate-date confirmation.
- Water-manager pool and request queue, and side-by-side proportional, urgency-first, and equal-share (max-min) allocation proposals. The manager decides and supplies a reason.
- Pool checks in serialized database transactions, reasoned pool/decision changes, partial approvals, clarification, deliveries, receipts, discrepancy reports, and correction history. Farmers can edit or withdraw undecided requests; managers can correct an approved quantity with a reason.
- Farmer and manager ledgers from the same timestamped audit records. Request and supply-period events use SHA-256 hash chains; SQLite triggers reject audit-event updates and deletes.
- Phone-sized responsive screens, loading/error/empty states, and template calculation explanations.

The allocation support is deterministic rule-based calculation—not trained machine learning and not an objectively fair or optimal result. AI-generated explanation is not enabled in this demo; fixed templates explain only already-calculated numbers. The manager always has final authority.

## Run locally

Prerequisites: Python 3.10+ and Node.js 20+.

```bash
# Terminal 1 — backend, from repository root
python -m pip install -r backend/requirements.txt
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload

# Terminal 2 — frontend
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. The API health check is `http://127.0.0.1:8000/health`; interactive API docs are at `http://127.0.0.1:8000/docs`. The SQLite database is initialized and seeded at backend startup. Set `VITE_API_BASE_URL` in the frontend environment if the API is not available at `http://localhost:8000`.

## Illustrative demo accounts

Choose an account from the sign-in screen; there is no public sign-up.

| Role | Username | Demo password |
|---|---|---|
| Water Manager | `manager` | `water_manager` |
| Farmer A | `farmer-a` | `farmer_a` |
| Farmer B | `farmer-b` | `farmer_b` |
| Farmer C | `farmer-c` | `farmer_c` |

These credentials are for the local hackathon demonstration only. Passwords are stored as salted scrypt hashes, and sessions expire after eight hours. Do not reuse these accounts or credentials for a live deployment.

## Verification

```bash
PYTHONPATH=backend python -m pytest backend/tests -q
cd frontend && npm run build
```

The legacy field/forecast endpoints remain available under `/api/v1` for backward compatibility but are not part of the current MVP user flow. The new workflow is served at `/workflow` and `/api/v1/workflow`.

## Limitations and claims

All bundled names, accounts, requests, and quantities are illustrative, not verified farmer or supply data. Area and crop are self-declared and unverified. Recorded deliveries are not proof of physical water delivery. Hashes provide tamper evidence, not protection against a database administrator who can rebuild the chain. The demo has one service area and one manager; real farmer verification, appeals, multiple authorities, integrations, offline use, and field or satellite monitoring are out of scope. No claim is made about water savings, yields, fairness, or climate impact.
