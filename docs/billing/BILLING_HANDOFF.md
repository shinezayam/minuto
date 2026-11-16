# Minuto billing: developer handoff

**Status date:** 2026-11-16 · **Owner of this document:** handed over to the next developer · **Read this first**, then the linked documents.

> **TL;DR.** The landing-page **Pricing section is built, tested, committed and live**. **No billing backend exists yet**: no payments, no subscriptions, no minute metering, no enforcement, no migration `0005`. Everything about billing beyond the Pricing UI is a **reviewed plan**, not code. The first job is to get the Step 1 decisions approved (section 11), then build **shadow-mode metering** (flag default *off*), then QPay, then enforcement.

## Contents
1. [Where things are](#1-where-things-are)
2. [System architecture and production services](#2-system-architecture-and-production-services)
3. [Pricing plans and agreed billing rules](#3-pricing-plans-and-agreed-billing-rules)
4. [Implemented vs planned](#4-implemented-vs-planned)
5. [The Pricing section (the only shipped billing work)](#5-the-pricing-section-the-only-shipped-billing-work)
6. [Subscription and minute-metering architecture](#6-subscription-and-minute-metering-architecture)
7. [Database schema and migration 0005](#7-database-schema-and-migration-0005)
8. [Shadow-mode approach](#8-shadow-mode-approach)
9. [QPay integration: requirements and responsibilities](#9-qpay-integration-requirements-and-responsibilities)
10. [Payment verification and security requirements](#10-payment-verification-and-security-requirements)
11. [Decisions: resolved and still open](#11-decisions-resolved-and-still-open)
12. [Known issues and risks](#12-known-issues-and-risks)
13. [Prioritized development checklist](#13-prioritized-development-checklist)
14. [Testing, deployment and rollback precautions](#14-testing-deployment-and-rollback-precautions)
15. [Repository hygiene notes](#15-repository-hygiene-notes)
16. [Your first steps](#16-your-first-steps)

---
## 1. Where things are

| What | Where |
|---|---|
| This handoff | `backend/docs/billing/BILLING_HANDOFF.md` (branch `docs/billing-handoff`, see §15) |
| Billing plan (phases, flow, user-facing scope) | `docs/billing/BILLING_PLAN.md` |
| **Step 1 proposal**: edge cases, schema, **migration 0005 draft**, tests, rollback, **QPay contract** | `docs/billing/BILLING_STEP1_PROPOSAL.md` (authoritative for Step 1) |
| Migrations how-to | `docs/MIGRATIONS.md` |
| Deployment notes | `docs/DEPLOYMENT.md` |
| AI quality / evals | `docs/AI_QUALITY.md` |
| Pricing UI code | frontend repo `src/app/page.tsx` (`PLANS`, `PLAN_STYLE`, `Pricing`), `src/components/landing/landing-nav.tsx`, `src/i18n/dict/landing.ts` |
| Backend repo | `github.com/shinezayam/minuto` (public) · FastAPI · deploys to Render |
| Frontend repo | `github.com/shinezayam/minuto-frontend` (public) · Next.js · deploys to Vercel |
| **Private companion notes** (infrastructure findings, audits, production topology details, security-relevant issues) | **Not in Git** (both repos are public). Ask the previous owner; they hold `handoff-private/`. Section 12 marks what lives there. |

---
## 2. System architecture and production services

Minuto is a Mongolian-language AI meeting-minutes web app: users record or upload meetings; ASR produces transcripts; an LLM produces minutes (summary, decisions, action items with evidence).

**Frontend** (`minuto-frontend`): Next.js 16.4.3 (Turbopack, `cacheComponents`), React 19, Tailwind 4, lucide icons. Custom i18n (`useT()`; **keys are the Mongolian source strings**, English in `src/i18n/dict/*.ts`). Hosted on **Vercel** (project `mongolian-ai-minutes`). The API base URL comes from `NEXT_PUBLIC_API_BASE_URL`. **Read `AGENTS.md` in the frontend root:** this Next.js version differs from older ones; docs are in `node_modules/next/dist/docs/`.

**Backend** (`minuto`): FastAPI, SQLAlchemy 2 + Alembic, **PostgreSQL in production** (SQLite for dev and tests). Hosted on **Render**. Key parts:
- Auth: email+password (PBKDF2), JWT, short-lived tickets for WebSockets, rate limits.
- Meetings API (`app/api/meetings.py`), live recording WebSocket (`app/api/websocket.py`), live preview transcriber (`app/services/live_transcriber.py`).
- **Durable job queue** (`app/services/jobs.py`): in-process workers (`JOB_WORKERS`, default 2), one active job per meeting enforced by a unique partial index, leases/heartbeats, retries with backoff, per-chunk ASR checkpoints in `processing_jobs.chunks`.
- Processing pipeline (`app/services/processing.py`): ASR (Qwen-Omni via tokenhub.mn, 30-second chunks) → save transcript → LLM minutes → save.
- Audio storage (`app/services/audio_store.py`): local disk (`AUDIO_STORAGE_PATH`; production uses a persistent disk, 5 GB at last report) with optional private Supabase Storage (**implemented, not activated**).
- Upload/record guards (`app/services/upload_guard.py`): size, type, 4-hour max, quotas, free-disk check.
- Workspaces with roles `owner/admin/editor/viewer` (`app/services/access.py`, `workspaces.py`).
- Migrations: `app/migrations/versions/0001…0004`, **current head `0004`**, run **automatically at app startup** (`app/db.py` → `upgrade_to_head`), serialized by a Postgres advisory lock.

**Production services (public facts)**
| Service | Host |
|---|---|
| Frontend (production) | `https://mongolian-ai-minutes.vercel.app` (Vercel team `shinezayas-projects`) |
| Backend (production) | `https://minuto-ai-meeting-assistant.onrender.com` (Render web service + Render PostgreSQL) |
| Optional audio store | Supabase Storage, private bucket: **not activated** |
| ASR / LLM provider | tokenhub.mn (Qwen); key only in Render env vars |
> ⚠ **A second, legacy frontend+backend pair is still live.** Its details, and the unverified question of whether it shares the production database, are in the private notes. **Do not assume it is inactive.** Because both backends auto-deploy the same repo, **a merged migration runs against both** (see §12).

**Deployment behaviour:** pushing to `main` auto-deploys both repos (Vercel for the frontend; Render for the backend). Backend startup runs pending migrations. Pushing to any *other* branch does not deploy.

**Local development:** backend on `:8000` (`uvicorn app.main:app`), frontend on **`:3001`** (the maintainer's machine has another app on `:3000`); backend `CORS_ORIGINS` must include the frontend origin. Tests must run with demo providers so no paid API is called:
```bash
cd backend && ASR_PROVIDER=demo LLM_PROVIDER=demo QWEN_API_KEY= .venv/bin/python -m pytest -q     # 319 passing at handoff
cd frontend && pnpm exec tsc --noEmit && pnpm exec vitest run                                      # 38 passing at handoff
```
Playwright E2E (46 tests) uses an isolated local stack, see `frontend/e2e/README.md`. **Do not run `pnpm build` while `next dev` is running**: both write `.next`, and the dev server then serves broken chunks ("module factory is not available"). Stop dev, build, then restart.

---
## 3. Pricing plans and agreed billing rules

| Plan | Price | Allowance | Scope (proposed) |
|---|---:|---|---|
| Free | 0₮ | **30 minutes, one-time** | personal |
| Starter | 29,900₮ | 120 min / 30-day period | personal |
| Pro | 69,900₮ | 360 min / 30-day period | personal |
| Team | 149,900₮ | 900 min / 30-day period, **shared across the workspace** | workspace |
| Enterprise | custom | custom | quote request (contact form) |

**Agreed rules (from the product owner):**
1. Free: one-time 30-minute allowance.
2. Paid plans: **manually renewed every 30 days** unless *official* QPay documentation confirms recurring billing (none found; see §9).
3. Rounding: audio duration **rounded up to whole minutes per meeting**.
4. **Failed transcription: no charge.**
5. **Reprocessing the same audio: no extra charge.**
6. Team: 900 minutes shared across the workspace.
7. **Unused minutes do not roll over.**
8. Expired subscription: no new paid allowance; **existing meetings, transcripts and exports stay available**.
9. Exhausted minutes: **block new transcription jobs, but never abruptly stop an ongoing recording.**
10. Payment provider: **QPay**.
11. Upgrade: allowed; **price-difference rule deliberately not decided yet**.
12. Cancel: use the plan until the paid period ends.
13. Refunds: **separate, not yet written terms**; the product must not mention refund/upgrade-difference wording until approved.
14. Enterprise: a quote-request form (needs an inbox address).

---
## 4. Implemented vs planned

| Item | State |
|---|---|
| Landing-page Pricing section (5 plans, nav link, English strings) | ✅ **Done, tested, live** (frontend commits `32e4dfc`, `a98c2be`) |
| Free CTA → registration | ✅ live (`/login?mode=register`) |
| Paid and Enterprise CTAs | ⏸ intentionally **disabled** with a visible note; no checkout exists |
| Plans/subscriptions/usage tables and migration `0005` | 📝 draft **in the Step 1 proposal only**; not in any repo |
| Minute metering (shadow or enforce) | 📝 planned |
| Feature flag `BILLING_METERING` | 📝 planned (default `off`) |
| QPay adapter, orders, checkout, callback | 📝 planned |
| Billing UI (checkout page, usage meter, settings, contact form) | 📝 planned |
| Minute enforcement (`402 MINUTES_EXHAUSTED`) | 📝 planned (last step) |
| Existing-user free allowance | 📝 designed as *computed*, no backfill rows needed (§6) |

**Nothing billing-related touches recording, live transcription, processing or AI summaries today.**

---
## 5. The Pricing section (the only shipped billing work)

- `src/app/page.tsx`: `PLANS` (data), `PLAN_STYLE` (light "default" and dark variants), `Pricing()` (section `id="pricing"`, placed between *Use cases* and *FAQ*). Layout: 4 plans in a row + Enterprise as a full-width dark band (`lg`), 2+2+1 on tablet, one column on mobile. **Pro** uses the dark style with a primary ring (no badge). Hero-style glow/grid backdrop and gradient heading.
- `src/components/landing/landing-nav.tsx`: a `#pricing` link; the **desktop nav now shows from `lg` (1024px)** because the extra link wrapped on tablet. `scroll-mt-20` offsets the 64px sticky header.
- `src/i18n/dict/landing.ts`: English strings under `// Pricing`.
- Only the Free CTA is a link. Paid CTAs are `<button disabled aria-describedby="pricing-note">` with the note "Төлбөртэй багцын захиалга одоогоор нээгдээгүй байна."
- **When billing goes live:** give `Plan` an `href` (e.g. `/checkout?plan=pro`) or an onClick, delete the note, and add the Enterprise contact flow. Keep prices/minutes in sync with the `plans` table (a test should compare them, see §14).
- Verified before shipping: typecheck, 38 unit tests, production build, axe (zero WCAG A/AA violations), no horizontal overflow at 1280/1100/800/390px, prices match the approved table.
- **Copy TODO:** the page shows Free as "30 минут" with no period; the agreed rule is "one-time". Consider "30 минут (нэг удаа)" and a Mongolian/English pair in the dictionary.

---
## 6. Subscription and minute-metering architecture

Full detail: `BILLING_STEP1_PROPOSAL.md` §B–§C. Summary:

- **Unit:** `billable_minutes = ceil(audio_seconds / 60)`, minimum 1 for non-empty audio. **Measure the final audio file** (ffprobe); `meetings.duration_ms` is the recording clock and is `0` for uploads, so it is not reliable.
- **Account debited:** the meeting's workspace if it has an active **Team** subscription; otherwise the **meeting owner's** personal allowance.
- **Allowance arithmetic (no grant rows):**
  - Free remaining = `30 − SUM(billable_minutes where plan_code='free' and state in (reserved, debited))` per user. Existing users therefore get the 30 minutes automatically.
  - Paid remaining = `plan.minutes − SUM(events in the current period)`; a new period starts a fresh sum, so **no rollover** needs no extra code.
- **Reserve → settle/release:** reserve at `POST /process` (before `try_begin_processing`); settle in `jobs.execute` after the job ends: **debit** if a transcript was saved, **release** otherwise.
- **Idempotency:** key `(meeting_id, SHA-256 of the audio file)` with a partial unique index over `state IN ('reserved','debited')`. Duplicate requests, worker retries, crash recovery and **reprocessing identical audio** never double-charge; changed audio is charged anew.
- **Concurrency:** reservation under a short per-account lock (PostgreSQL `pg_advisory_xact_lock(hashtext(scope))`; SQLite `BEGIN IMMEDIATE`), so two simultaneous jobs cannot both spend the last minutes.
- **Partial failures:** ASR failure/timeout/crash/`NO_SPEECH`/restart → release (no charge). Transcript saved but summary failed → **debit stays** (decision pending, §11). A reconcile sweep (startup + every 10 min) releases orphaned reservations.
- **Recording longer than what's left:** new starts at 0 minutes are refused; a recording already in progress is **never stopped**; a finished live recording may **overdraft once**; an upload longer than what's left is **refused up front with the exact shortfall** (confirm in §11). Overdraft does not carry over.
- **Hook points (existing code):** `api/meetings.py:process_meeting` (reserve), `services/jobs.py:execute` (settle), startup + timer (reconcile). **No WebSocket change in Step 1.**
- **Cost note (not metered in Step 1):** live transcription calls the paid ASR during recording and processing then transcribes the whole file again (~2× ASR calls for live meetings).

---
## 7. Database schema and migration 0005

The complete draft Alembic migration, the equivalent PostgreSQL DDL, indexes and the downgrade are in **`BILLING_STEP1_PROPOSAL.md` §D**. Not committed as code on purpose. Overview:

| Table | Purpose |
|---|---|
| `plans` | the five plans, seeded with exactly the approved prices/minutes (`free 30`, `starter 120 / 29,900`, `pro 360 / 69,900`, `team 900 / 149,900`, `enterprise` custom) |
| `subscriptions` | per user **or** per workspace (`CHECK` exactly one owner); `status`, `period_start/end`, `cancel_at_period_end`; partial unique index: one **active** subscription per owner |
| `usage_events` | ledger-style, **no foreign keys** (history survives deletion of users/meetings): scope, meeting, job, plan at the time, source (`live/upload`), audio seconds, billable minutes, fingerprint, `state` (`reserved/debited/released`), `shadow`, `decision` (`allowed/would_block/would_overdraft/free_reprocess`), `remaining_before`, timestamps; **partial unique index on `(meeting_id, audio_fingerprint)` for live states** |

Later steps add `payment_orders` and `contact_requests` (not in 0005).
Implementation checklist for the migration: add the matching ORM models to `app/db.py` (the existing **drift test** `tests/test_migrations.py::test_models_and_migrations_do_not_drift` must stay green); extend `test_migrations.py` (fresh → `0005`, idempotent upgrade, downgrade `0005→0004` keeps all data, existing DB with data, plan rows equal the approved table). Revision id `0005`, `down_revision = "0004"`.

---
## 8. Shadow-mode implementation approach

Flag: `BILLING_METERING = off | shadow | enforce`, **default `off`**; Step 1 implements `off` and `shadow` only (`enforce` does not exist yet).
- `off`: tables exist, **no code path touches them** (assert in a test: zero billing queries).
- `shadow`: record what *would* be charged/blocked; **never block, delay or alter any response**. Wrap every metering call so an exception is logged and swallowed; **test this by injecting failures** into every billing function and asserting `/process`, the worker and the WebSocket behave exactly as before.
- Rollout: deploy with the flag unset (identical behaviour) → verify → set `shadow` on **one** backend in a quiet window → observe `usage_events` for about a week → use `would_block`/`would_overdraft` counts to confirm the policies in §11 before enforcement.

---
## 9. QPay integration: requirements and responsibilities

**Boundary:** the *adapter* (`app/billing/payments/qpay.py`) talks to QPay and never touches the database; *Minuto's billing service* owns orders, subscriptions, the ledger and HTTP routes and never speaks HTTP to QPay. The interface (`PaymentProvider`, `OrderRef`, `InvoiceResult`, `PaymentCheck`, errors `ProviderUnavailable`/`ProviderRejected`) and the shared **contract test suite** are specified in `BILLING_STEP1_PROPOSAL.md` §G. A `FakeProvider` implementing the same interface is used in CI.

**Flow:** `POST /api/billing/checkout {plan}` → order `pending` (amount from the `plans` table, never from the client) → `create_invoice` → QR + bank deep links → user pays → QPay calls `POST /api/billing/qpay/callback/{secret}` → **always** `check_payment` → only if `paid` **and** amount equals the order → `apply_paid_order` (one transaction, idempotent) → subscription/period created; `GET /api/billing/orders/{id}` polls while `pending`. Order states: `pending → paid | expired | failed | cancelled`; `paid` is immutable. Early renewal extends from `max(now, current period_end)`.

**Facts and caveats about QPay (verify all):**
- Third-party mirrors (not QPay's official portal) describe: token via `POST /v2/auth/token` (Basic auth; access ≈10 min, refresh ≈1 h), `POST /v2/invoice` with a `callback_url`, `POST /v2/payment/check`, `DELETE /v2/invoice/{id}`; sandbox host separate from production.
- **No recurring/subscription billing was found.** Default is a **manual 30-day invoice each month** with a "Renew" button and reminders. **Ask QPay in writing** whether recurring/card-on-file exists for merchants.
- **Every endpoint path, field name, the sandbox host and callback behaviour must be confirmed against QPay's official merchant documentation or your account contact before coding.** Nothing was ever called against QPay.
- Credentials (`QPAY_BASE_URL`, `QPAY_USERNAME`, `QPAY_PASSWORD`, `QPAY_INVOICE_CODE`, `QPAY_CALLBACK_SECRET`, plus sandbox set): **entered by the owner directly in Render's environment**, never in Git, chat or logs.

---
## 10. Payment verification and security requirements
1. **Never trust the callback.** It only identifies an invoice; the order becomes `paid` solely after a server-side `check_payment` returns paid **and** the paid amount equals the order amount **and** the invoice belongs to that order.
2. Callback URL carries a high-entropy secret; compare in constant time; rate-limit the route; unknown invoices are ignored with a generic response.
3. **Idempotent application:** duplicate callbacks/polls cannot double-grant (unique order state transition + the active-subscription unique index).
4. Amounts and prices come only from the server `plans` table; integer MNT; no float money.
5. No card or bank data ever reaches Minuto; no provider response bodies or credentials in logs or exceptions.
6. Authorization on every billing endpoint; Team purchase and cancellation restricted to workspace **owner/admin** (confirm in §11).
7. Audit: keep raw `check_payment` results on the order for reconciliation; build a reconciliation report (QPay vs orders) before going live.
8. Provider outages must degrade safely: checkout shows a retryable error, nothing is half-activated.

---
## 11. Decisions: resolved and still open

**Resolved (owner):** QPay; Free one-time 30; manual 30-day renewal unless QPay documents recurring; rounding up per meeting; failed transcription free; reprocessing same audio free; Team shared per workspace; no rollover; expired plans keep data/exports; never stop an ongoing recording; upgrades allowed (difference rule later); cancel = use until period end; refunds separate; Enterprise = quote request.

**Open: need an owner/product decision before or during Step 1**
1. Approve the **Step 1 scope** (three tables, shadow only, flag default `off`).
2. **Overdraft policy:** live recordings may overdraft once; uploads are refused when they exceed the remainder (or choose strict/overdraft for both).
3. **Summary failure after a good transcript:** keep the debit (recommended) or release.
4. **Editor in a workspace without Team:** charge the meeting owner (proposed).
5. **Team plan ownership:** who may buy/cancel (owner/admin?), and what happens to the allowance if the buyer leaves.
6. **Which backends/databases are real** and whether the legacy pair is retired (private notes). Billing must read one database.
7. **Backups:** confirm a database backup is available before the migration deploy.
8. **Upgrade price difference** and **refund terms** (the UI must stay silent on both until approved); also terms-of-service/privacy wording and any invoice/receipt requirements.
9. **QPay:** merchant account + sandbox credentials; written answer on recurring billing; invoice expiry time.
10. **Enterprise contact:** destination inbox and mail provider (or an admin list in the app).
11. **Pricing copy:** show "(нэг удаа)" for Free?

---
## 12. Known issues and risks

**Billing-specific**
- **Deploy = migration.** Migrations run automatically at startup on every backend that deploys the commit. There is no "hold the migration" switch once merged. Plan a window and a backup (§14).
- **Rollback trap.** If the database is at `0005`, **older code will refuse to start** (unknown revision). Correct rollback: flag off → `python -m app.migrate downgrade 0004` (drops only the new tables) → *then* roll back the deploy.
- **Two live backends** auto-deploy the same repo; a single migration runs on both. Enforcement must be enabled on exactly one or the second retired after a user migration.
- Metering depends on accurate duration probing; define behaviour when ffprobe cannot read the file (shadow records `unknown`, charges nothing).
- Enforcement can lock users out: hence shadow mode, the automatic free allowance and the flag.

**Platform (in the private notes; summarized, not detailed here)**
- Audio disk usage, long-meeting processing and storage-exhaustion handling had a production incident; fixes and a cleanup lifecycle are planned and **interact with metering** (incremental ASR changes the processing flow where `reserve/settle` hook in).
- Durable audio (Supabase) is implemented but not activated; upload verification and the remote-delete queue are incomplete.
- Stale services/domains, leftover synthetic test accounts on production, outdated names in docs/`render.yaml`.
- CI workflow templates exist in `backend/ci/` and `frontend/ci/` but are **not active** (the GitHub token used lacked the `workflow` scope).

---
## 13. Prioritized development checklist

**P0: before writing code**
- [ ] Read §1 documents; skim `app/services/jobs.py`, `processing.py`, `api/meetings.py:process_meeting`.
- [ ] Get answers to §11 items 1–7 in writing.
- [ ] Verify production backend ↔ database mapping (private notes list the read-only commands).
- [ ] Take/confirm a production database backup procedure.

**P1: Step 1, shadow metering (backend, one PR, no enforcement)**
- [ ] ORM models + migration `0005` (+ tests: fresh, drift, idempotent, downgrade, existing data, plan rows).
- [ ] `app/billing/` package: `scope.py` (account resolution), `meter.py` (reserve/settle/release/reconcile, fingerprint, rounding), flag `BILLING_METERING` in `config.py`.
- [ ] Hooks in `process_meeting` and `jobs.execute`; reconcile at startup + timer; every call wrapped (swallow + log).
- [ ] Tests from `BILLING_STEP1_PROPOSAL.md` §E (rounding, concurrency, idempotency, failure matrix, policy decisions, failure-injection safety). The 319 existing tests must pass unchanged.
- [ ] Review → backup → merge in an approved window → verify `current == 0005` and `/api/readyz` → set `shadow` on one backend → observe a week.

**P2: Step 2, QPay (sandbox)**
- [ ] Confirm API details against official docs; `PaymentProvider` interface + `FakeProvider`; contract tests.
- [ ] `payment_orders` migration `0006`; checkout/order/callback/cancel routes; `apply_paid_order`; reconciliation report.
- [ ] Sandbox end-to-end test with a synthetic account.

**P3: Step 3, frontend**
- [ ] `/checkout` page (QR, bank links, polling, success/expired); billing section in Settings (plan, minutes left, period end, renew, cancel at period end, history); minutes meter; "minutes exhausted" state; Enterprise contact form.
- [ ] Wire Pricing CTAs behind a flag; EN/MN strings; Playwright tests; axe.

**P4: Step 4, enforcement and launch**
- [ ] Add `enforce` mode (`402 MINUTES_EXHAUSTED`; live recordings never stopped); confirm policies with shadow data.
- [ ] Production QPay credentials (owner enters them); launch on one backend; monitor failed payments, unmatched callbacks and ledger totals for a week.

**P5: later**
- [ ] Upgrade proration and refund policies once approved; invoices/receipts; reminders before period end; metering of live-ASR cost if desired.

---
## 14. Testing, deployment and rollback precautions

**Run before every PR**
```bash
cd backend && ASR_PROVIDER=demo LLM_PROVIDER=demo QWEN_API_KEY= .venv/bin/python -m pytest -q   # never run tests against the real .env/providers
cd frontend && pnpm exec tsc --noEmit && pnpm exec vitest run && pnpm exec eslint <changed files>
```
Add a test that the five seeded plan rows equal the approved price table, and a frontend test (or snapshot) that `PLANS` matches it.

**Deploying**
- Backend `main` push = Render auto-deploy + startup migration. **Do not merge billing migration code without the approvals in §11 and a backup.** Prefer a quiet window (a deploy restarts the service, about 1 minute of 502s).
- Frontend `main` push = Vercel production deploy. Build locally first (stop `next dev` before `pnpm build`).
- Pushing a non-`main` branch does not deploy.
- Set env vars only in the Render/Vercel dashboards; never commit `.env`, keys, QPay credentials or database URLs (backend `.env` is git-ignored).

**Rollback**
- *Metering:* set `BILLING_METERING=off`, no data change.
- *Schema:* `python -m app.migrate downgrade 0004` in the Render Shell **before** rolling the code back (see §12); the downgrade of each new revision is covered by `test_migrations.py`.
- *Failed startup migration:* PostgreSQL DDL is transactional, so the database stays at the previous revision and Render keeps the last healthy deploy running; roll back the commit.
- *Frontend:* Vercel → previous deployment → promote, or revert the commit.

---
## 15. Repository hygiene notes
- **Branches:** `main` is clean and in sync with `origin/main` in both repos at handoff. The documents in `docs/billing/` are on the backend branch **`docs/billing-handoff`** (pushed, **not merged**, no deploy). Merging it to `main` triggers a Render redeploy of unchanged code (about 1 minute restart); do it deliberately.
- **Obsolete local-only branch** in the backend: `hotfix/disk-guard-threshold` (lowers a disk threshold to 100 MB). It was **never pushed or deployed** and is superseded by enlarging the production disk; do not deploy it. Delete when convenient.
- Old feature branches (`feature/local-mlx-asr` backend, `feature/landing-and-transcript-editor` frontend) are historical.
- **Local, git-excluded probes** in the previous owner's frontend checkout (`e2e-prod/`, `playwright.prod.config.ts`): production verification scripts that **create accounts on production**. Do not run them casually.
- `PRODUCTION_READINESS_TODO.md` (roadmap with a dated progress log), `E2E_REPORT.md`, `SETUP_CHECKLIST.md` and the reliability/storage reports are in the private bundle, not in Git.
- Commit style: end commit messages with the co-author trailer in use; one logical change per commit; push to `main` only when a deploy is intended.

---
## 16. Your first steps
1. Read this file, then `BILLING_STEP1_PROPOSAL.md` (sections B, C, D, G especially).
2. Ask the previous owner for the **private handoff bundle**; read its production-topology notes.
3. Run the baseline: backend tests (319), frontend typecheck + unit tests (38), and Playwright E2E if you will touch UI.
4. Send the owner the §11 questions (items 1–7 first) and the QPay account/sandbox request; ask QPay about recurring billing and the official API docs.
5. Create a branch `billing/step1-shadow-metering` from `main`; implement **only** P1 behind the flag; open a PR (**do not merge**) with test results.
6. Schedule the merge window and backup with the owner; follow §14.
