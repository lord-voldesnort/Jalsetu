# JalSetu AI MVP implementation plan

Replace the superseded field-monitoring prototype as the primary experience with the supplied v2 product: transparent farmer requests, manager decisions under a declared supply pool, delivery tracking, and shared event history. Reuse the existing React/Vite and FastAPI/SQLAlchemy stack; retain existing legacy routes but remove maps, forecasts, synthetic field data, and third-role controls from the user-facing workflow.

## Architecture and project structure
- `backend/app/db/models.py`: preserve legacy tables and add service area, demo users/sessions, supply periods, requests, deliveries, and append-only audit events.
- `backend/app/api/v1/endpoints/water_workflow.py`: authenticated demo login, role-scoped workflow endpoints, allocation support, and ledger verification.
- `backend/app/services/request_allocator.py`: deterministic proportional, urgency-first, and equal-share allocation over whole m³, with stable rounding.
- `backend/app/db/seed.py`: idempotently seed one illustrative service area, one manager, three farmers, and a declared demo supply period.
- `frontend/src/App.tsx`, `api/client.ts`, and `index.css`: mobile-first two-role request/manager experience driven by the new API.

Authorization is checked server-side on every workflow endpoint. SQLite serializes pool-affecting decision transactions using an immediate write transaction; other supported SQL dialects use a supply-period row lock. Audit records use per-request SHA-256 chains and SQLite triggers reject updates/deletes. AI text generation is not represented as implemented; deterministic template explanations are labelled accordingly.

## Design
Reuse the repository’s water-inspired blue/green identity, but simplify it into a calm civic-service console: one clear request queue, compact supply accounting, human-readable decision reasons, and visible audit integrity. Mobile-first stacked cards and restrained animation keep the workflow practical on phones. Brand essence: a transparent allocation record for farmers and water managers; clear, accountable, grounded. The mark is a water-drop/bridge motif, with freshwater blue as signature color.
