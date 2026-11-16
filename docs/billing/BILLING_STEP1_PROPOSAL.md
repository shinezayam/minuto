# Billing Step 1 (shadow metering): audit findings and proposal for approval

Date: 2026-11-16. **Nothing in this document has been executed.** No migration, data change, deploy, env change or enforcement. The migration below is a draft *inside this document*; it is not in any repo.

> **Handoff note:** this proposal was written for the product owner during planning, so "you" means the owner. Decisions it asks for are tracked in `BILLING_HANDOFF.md` §11. The production-hostname detail of the original section A was moved to the private handoff notes (this repository is public).

---
## A. Production backends and databases (summary; details are in the private notes)

- The production frontend calls the production backend (hostnames in `BILLING_HANDOFF.md` §2).
- A **second, legacy backend/frontend pair is still live.** Both backends **auto-deploy this repository** and **migrations run automatically at app startup** (`app/db.py: upgrade_to_head`). Therefore **merging migration 0005 to `main` executes it on every backend that deploys it, against its own database.** There is no separate "run migration" step to hold back; the merge/deploy *is* the migration.
- **Whether the two backends share one PostgreSQL instance is unverified** (`/api/readyz` only says `database: postgresql`). Read-only check, run on each backend in the Render Shell (prints host and database name only, never the password):
  ```bash
  python3 - <<'PYEOF'
  import os, urllib.parse as u
  r = u.urlparse(os.environ["DATABASE_URL"]); print("db host:", r.hostname, " db name:", r.path.lstrip("/"), " user:", r.username)
  PYEOF
  ```
  Same host and name on both = shared database. Also run `python -m app.migrate current` on each (expect `0004`).
- **Consequence for billing:** a user's balance must come from exactly one database. If the databases are separate, so are the user bases and balances. Enable enforcement on one backend only, or retire the second after a user migration, before Step 4.

---
## B. Edge-case investigation (from the code, not assumptions)

Code facts that drive the design:
- **Duplicate `POST /process`:** `try_begin_processing` is an atomic conditional UPDATE (status not already processing/recording/paused), and `processing_jobs` has a unique partial index allowing only one `queued/running` job per meeting. A duplicate gets `409 ALREADY_PROCESSING`. The job queue is already idempotent.
- **Retries and crash recovery:** the same job row is re-claimed (`attempts+1`) after a transient provider error (`RetryLater`, backoff) or lease expiry; finished ASR chunks are checkpointed in `processing_jobs.chunks`. A retry is **not** a new job.
- **Concurrency:** `job_workers = 2` (default), so two meetings, possibly the same user's, run simultaneously. Each reads its own state; nothing today coordinates a per-user total.
- **Duration is not reliably stored:** `meetings.duration_ms` is the *recording clock* for live recordings. For uploads it is `0` (the upload route probes duration only to enforce the 4-hour limit and discards it). **Metering must measure the final audio file itself** (ffprobe) at process time.
- **Live transcription spends provider calls during recording** (`live_transcriber → transcribe_wav_bytes`), and processing then transcribes the whole file **again**. So real ASR cost is roughly 2× the audio length for live meetings. Step 1 will not meter live windows (they are a preview), but I flag the cost.
- **Recording limits today:** a recording can run up to `max_recording_sec` = 4 hours; the WebSocket only checks disk and storage quota at connect. Nothing stops a recording mid-stream (and must not, per your rule 9).
- **Partial results:** transcript is saved to the database *before* summarization. If the LLM step fails the meeting ends `failed`/`SUMMARY_FAILED` but the transcript exists, and `regenerate-summary` re-runs only the LLM (no ASR).
- **Billing owner:** meetings have `owner_id` and optional `workspace_id`; workspaces have `owner_id`, `workspace_type` (personal/organization) and members with roles.

### B1. Recording longer than the remaining allowance
| Case | Proposal | Why |
|---|---|---|
| **Starting a new recording or upload with 0 minutes left** | Refuse at start with a clear message (`MINUTES_EXHAUSTED`) before any audio is accepted | This is a "new" job (your rule 9) |
| **Live recording in progress when minutes run out** | **Never stop it.** The recording and live preview continue to the end (max 4 h) | Your rule 9; the audio cannot be recreated |
| **Processing a finished live recording that is longer than what's left** | **Allow it in full** and let the balance go negative (one-time overdraft, capped by the 4-hour limit); further jobs stay blocked until renewal | Rejecting would lock a recording the user can no longer re-capture |
| **Processing an uploaded file longer than what's left** | **Refuse before starting** with the exact shortfall ("needs 47 min, 12 left") | The user still has the file and can upgrade or trim it |
| **Overdraft at renewal** | Does **not** carry over (new period resets the allowance) | Consistent with "no rollover" |
`source` (live vs upload) is derivable today: an `uploaded.*` file vs numbered chunks. **NEEDS DECISION:** confirm this live-vs-upload asymmetry, or choose strict-for-both or overdraft-for-both. Step 1 records `would_block` for each case so we can see real frequency before choosing.

### B2. Concurrent transcription jobs
Two simultaneous jobs for the same account can both see "30 minutes left" and both start. Fix: an **atomic reservation** inside one short transaction that (1) takes a lock on the account (PostgreSQL `pg_advisory_xact_lock(hashtext(scope))`; SQLite `BEGIN IMMEDIATE`), (2) computes `remaining = allowance - SUM(reserved+debited in period)`, (3) inserts the reservation. The second job then sees the first's reservation. Reservations are cheap, so the lock is held for milliseconds.

### B3. Duplicate requests and retries
Idempotency key = `(meeting_id, audio_fingerprint)` where the fingerprint is the SHA-256 of the final audio file (streamed in 1 MB blocks; about 0.3 s for 130 MB, done off the event loop). A **partial unique index** over `state IN ('reserved','debited')` makes a second reserve for the same audio return the existing record: **no second charge** (your rules 4 and 5), and a worker retry or crash-recovery re-claim reuses the same reservation. Replacing the audio changes the fingerprint, so it is charged as new, as you specified.

### B4. Partial failures: when is a minute actually charged?
| Outcome | Reservation | Reasoning |
|---|---|---|
| Job fails in ASR (provider error after all attempts, timeout, crash, `INTERNAL_ERROR`, `NO_AUDIO`, interrupted by restart) | **Release** (no charge) | Rule 4 |
| `NO_SPEECH` (transcription ran, found no speech) | **Release** | No usable transcript |
| ASR succeeds and the transcript is saved; summary then fails | **Debit stays** | A transcript was delivered and its provider cost incurred; `regenerate-summary` is free |
| ASR succeeds, summary succeeds | **Debit** | Normal |
| Meeting deleted mid-job | **Release** (reaper) | Nothing delivered |
**NEEDS DECISION:** the "summary failed after a good transcript" row is the only judgment call (alternative: release and charge nothing). Orphan protection: a **reconcile** job (at startup and every 10 min) releases any `reserved` row whose meeting has no queued/running job and is older than `job_timeout + lease`.

---
## C. Step 1 scope: shadow metering only

Everything is behind `BILLING_METERING = off | shadow | enforce`, **default `off`**. Step 1 supports `off` and `shadow`; `enforce` does not exist yet.
- `off`: new tables exist, no code path touches them.
- `shadow`: on `POST /process` and at job completion the system **records** what it would charge and what it would have blocked; it **never** blocks, delays or changes behavior. Every metering call is wrapped so any exception is logged and swallowed; processing is unaffected by construction.

Hook points (names refer to existing code):
1. `api/meetings.py:process_meeting`, after `has_audio` and before `try_begin_processing`: `billing.reserve(meeting_id)` (computes audio seconds and fingerprint in a thread; records the decision).
2. `services/jobs.execute`, after `complete(...)`: `billing.settle(job_id, outcome)`; outcome comes from whether `processing.run` saved a transcript.
3. Startup and a 10-minute timer: `billing.reconcile()`.
No change to the WebSocket in Step 1.

### Free-allowance accounting (no per-user grant rows)
Free "30 minutes one-time" = `30 - SUM(billable_minutes where plan_code='free' and state in (reserved,debited))` per account. Each event stores the plan in force when it was charged, so lapsed paid plans and the free pool cannot mix. Existing users automatically get the 30 (there is no grant to backfill). Paid allowance = `plan.minutes - SUM(events in the current period)`; no rollover falls out of it, because a new period starts a fresh sum.

### Which account is debited
If the meeting's workspace has an active Team subscription: the **workspace** (shared 900 minutes). Otherwise the **meeting owner's personal** allowance. (**NEEDS DECISION:** an editor processing a meeting in a workspace *without* Team is charged to the meeting owner, not the editor. Confirm.)

### Minute rounding
`billable_minutes = ceil(audio_seconds / 60)`, minimum 1 for any non-empty audio (59.9 s → 1; 60.0 s → 1; 60.1 s → 2). If duration cannot be probed: shadow records `duration_source='unknown'`, charges nothing, and flags it.

---
## D. Schema (Step 1)

Three new tables, all additive; no existing table or column changes. (Payment orders and contact requests arrive in Steps 2–3.)

### D1. Alembic migration draft (`0005_billing_shadow_metering.py`, **not in the repo**)
```python
"""billing shadow metering: plans, subscriptions, usage_events

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_LIVE = sa.text("state IN ('reserved','debited')")
_ACTIVE_SUB = sa.text("status = 'active'")


def upgrade() -> None:
    plans = op.create_table(
        "plans",
        sa.Column("code", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),          # free | paid | custom
        sa.Column("minutes", sa.Integer(), nullable=True),          # allowance; NULL = custom
        sa.Column("period_days", sa.Integer(), nullable=True),      # NULL = one-time or custom
        sa.Column("price_mnt", sa.Integer(), nullable=True),        # NULL = custom
        sa.Column("scope", sa.String(10), nullable=False),          # user | workspace
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.bulk_insert(plans, [
        {"code": "free",       "name": "Free",       "kind": "free",   "minutes": 30,  "period_days": None, "price_mnt": 0,      "scope": "user"},
        {"code": "starter",    "name": "Starter",    "kind": "paid",   "minutes": 120, "period_days": 30,   "price_mnt": 29900,  "scope": "user"},
        {"code": "pro",        "name": "Pro",        "kind": "paid",   "minutes": 360, "period_days": 30,   "price_mnt": 69900,  "scope": "user"},
        {"code": "team",       "name": "Team",       "kind": "paid",   "minutes": 900, "period_days": 30,   "price_mnt": 149900, "scope": "workspace"},
        {"code": "enterprise", "name": "Enterprise", "kind": "custom", "minutes": None, "period_days": None, "price_mnt": None,   "scope": "workspace"},
    ])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True),
        sa.Column("plan_code", sa.String(20), sa.ForeignKey("plans.code"), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),         # active | expired
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(user_id IS NULL) <> (workspace_id IS NULL)", name="ck_subscription_one_owner"),
    )
    op.create_index("ix_subscriptions_period_end", "subscriptions", ["period_end"])
    op.create_index("uq_subscriptions_active_user", "subscriptions", ["user_id"], unique=True,
                    sqlite_where=_ACTIVE_SUB, postgresql_where=_ACTIVE_SUB)
    op.create_index("uq_subscriptions_active_workspace", "subscriptions", ["workspace_id"], unique=True,
                    sqlite_where=_ACTIVE_SUB, postgresql_where=_ACTIVE_SUB)

    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(36), primary_key=True),
        # Ledger-style: no foreign keys, so usage history survives meeting/user/workspace deletion
        sa.Column("scope_type", sa.String(10), nullable=False),     # user | workspace
        sa.Column("scope_id", sa.String(36), nullable=False),
        sa.Column("meeting_id", sa.String(36), nullable=True),
        sa.Column("job_id", sa.String(36), nullable=True),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("plan_code", sa.String(20), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(10), nullable=False),         # live | upload
        sa.Column("audio_seconds", sa.Integer(), nullable=True),
        sa.Column("duration_source", sa.String(10), nullable=False),  # probe | clock | unknown
        sa.Column("billable_minutes", sa.Integer(), nullable=False),
        sa.Column("audio_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),          # reserved | debited | released
        sa.Column("shadow", sa.Boolean(), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),       # allowed | would_block | would_overdraft | free_reprocess
        sa.Column("remaining_before", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_usage_events_scope", "usage_events", ["scope_type", "scope_id", "created_at"])
    op.create_index("ix_usage_events_state", "usage_events", ["state"])
    op.create_index("uq_usage_events_audio", "usage_events", ["meeting_id", "audio_fingerprint"], unique=True,
                    sqlite_where=_LIVE, postgresql_where=_LIVE)


def downgrade() -> None:
    op.drop_index("uq_usage_events_audio", table_name="usage_events")
    op.drop_index("ix_usage_events_state", table_name="usage_events")
    op.drop_index("ix_usage_events_scope", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_index("uq_subscriptions_active_workspace", table_name="subscriptions")
    op.drop_index("uq_subscriptions_active_user", table_name="subscriptions")
    op.drop_index("ix_subscriptions_period_end", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
```
(The matching ORM models go in `app/db.py` so the existing drift test `test_models_and_migrations_do_not_drift` stays green.)

### D2. Equivalent PostgreSQL DDL (for DBA review; Alembic generates this)
```sql
CREATE TABLE plans (
  code VARCHAR(20) PRIMARY KEY, name VARCHAR(50) NOT NULL, kind VARCHAR(10) NOT NULL,
  minutes INTEGER, period_days INTEGER, price_mnt INTEGER, scope VARCHAR(10) NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO plans (code,name,kind,minutes,period_days,price_mnt,scope) VALUES
 ('free','Free','free',30,NULL,0,'user'), ('starter','Starter','paid',120,30,29900,'user'),
 ('pro','Pro','paid',360,30,69900,'user'), ('team','Team','paid',900,30,149900,'workspace'),
 ('enterprise','Enterprise','custom',NULL,NULL,NULL,'workspace');

CREATE TABLE subscriptions (
  id VARCHAR(36) PRIMARY KEY,
  user_id VARCHAR(36) REFERENCES users(id) ON DELETE CASCADE,
  workspace_id VARCHAR(36) REFERENCES workspaces(id) ON DELETE CASCADE,
  plan_code VARCHAR(20) NOT NULL REFERENCES plans(code),
  status VARCHAR(10) NOT NULL, period_start TIMESTAMPTZ NOT NULL, period_end TIMESTAMPTZ NOT NULL,
  cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT ck_subscription_one_owner CHECK ((user_id IS NULL) <> (workspace_id IS NULL)));
CREATE INDEX ix_subscriptions_period_end ON subscriptions (period_end);
CREATE UNIQUE INDEX uq_subscriptions_active_user ON subscriptions (user_id) WHERE status = 'active';
CREATE UNIQUE INDEX uq_subscriptions_active_workspace ON subscriptions (workspace_id) WHERE status = 'active';

CREATE TABLE usage_events ( /* columns as in D1 */ );
CREATE INDEX ix_usage_events_scope ON usage_events (scope_type, scope_id, created_at);
CREATE INDEX ix_usage_events_state ON usage_events (state);
CREATE UNIQUE INDEX uq_usage_events_audio ON usage_events (meeting_id, audio_fingerprint)
  WHERE state IN ('reserved','debited');
```
Impact: three new, empty-except-`plans` tables. No locks on existing tables (only `CREATE`), no rewrite, so the startup migration takes milliseconds; the existing advisory lock (`pg_advisory_lock(727001)`) already serializes concurrent starts.

---
## E. Test plan (all runnable offline; no production access)
**Migration tests** (extend `test_migrations.py`): fresh DB reaches `0005`; models and migrations do not drift; upgrade is idempotent; downgrade `0005 → 0004` leaves every existing table and row intact; a pre-existing database with data upgrades with no data loss; the five plan rows match the approved price table exactly (regression guard for prices/minutes).
**Metering unit tests:** rounding table (0.1 s, 59.9 s, 60 s, 60.1 s, 3,600 s, 0 s/empty); fingerprint streaming (memory stays flat on a large generated file); free-allowance arithmetic; paid-period arithmetic and no-rollover; scope resolution (personal vs Team workspace; editor in a non-Team workspace).
**Concurrency tests:** two and ten simultaneous reserves near the limit, using threads against a file-based SQLite database (`BEGIN IMMEDIATE`) and, if available locally, a real PostgreSQL (advisory lock): the total never exceeds allowance + permitted overdraft; no deadlock; lock time is bounded.
**Idempotency tests:** duplicate `POST /process` (existing 409 path plus no second event); worker retry after `RetryLater`; lease-expiry re-claim after a simulated crash; reprocess of identical audio is a `free_reprocess`; changed audio is charged again.
**Failure-matrix tests** (one per row of B4): ASR provider failure, timeout, internal error, NO_SPEECH, restart interruption, summary-only failure, meeting deleted mid-job.
**Policy tests (shadow decisions):** 0 left → `would_block`; live longer than remaining → `would_overdraft`; upload longer than remaining → `would_block` with exact shortfall; an in-progress recording is never touched.
**Safety tests (most important):** with `BILLING_METERING=shadow`, inject exceptions into every billing function and assert `POST /process`, the job worker and the WebSocket behave exactly as before; with `off`, assert zero billing queries; the existing 319 backend tests pass unchanged.
**API/doc:** the OpenAPI snapshot test (`openapi_snapshot.py`) is regenerated only if I add the optional read-only `GET /api/billing/me`.
**Pre-deploy checklist (manual):** the migration runs on a copy of a production-shaped Postgres.

---
## F. Rollout and rollback

**Rollout (each line needs your explicit approval before it happens)**
1. I implement on a branch; you review the diff and the test results.
2. **Backup first:** `pg_dump` of the production database(s) (Render free Postgres has no automatic backups; confirm your plan).
3. Merge → both backends auto-deploy → migration `0005` runs at startup on each; `BILLING_METERING` is unset = `off`, so behavior is identical.
4. Verify (read-only): `python -m app.migrate current` shows `0005`; `/api/readyz` is ok; the five plan rows exist.
5. In a quiet window, set `BILLING_METERING=shadow` on **one** backend (~1 minute restart). Watch `usage_events` for a week.

**Rollback (this has one non-obvious trap)**
- *Stop metering instantly:* set `BILLING_METERING=off` (or remove it). No data change.
- ⚠ *Do not simply roll the code back while the database is at `0005`.* Older code does not know revision `0005`, and Alembic would refuse to start (an outage). Correct order: **(1)** with the new code still running, in the Render Shell run `python -m app.migrate downgrade 0004` (drops only the three new tables; shadow data is lost, user data is untouched; the downgrade is covered by a test); **(2)** then roll back the deploy.
- *If the migration itself fails at startup:* it runs inside the Alembic transaction on PostgreSQL (DDL is transactional), so a failure leaves the database at `0004`; the service fails to start on the new build, and **Render keeps serving the previous healthy deploy** until the new one is healthy (zero-downtime deploy). Roll back the deploy; nothing to undo in the database.
- *Both backends:* repeat on each. If their databases are shared, the migration runs once and the second backend sees `0005` already applied (the advisory lock and idempotent upgrade handle the race).

**Risks I'm flagging**
- Auto-migrate-on-deploy means "approve Step 1" = "approve a production schema change"; I'll ask for that exact approval again before merging.
- Two live backends doubles the deploy surface (see A).
- The fingerprint reads the whole audio once per process request (about 0.3 s per 130 MB); acceptable, and done off the event loop.
- Overdraft and charge-on-summary-failure rules are product decisions, listed below.

---
## G. QPay integration contract (for your colleague; Step 2, not Step 1)

**Boundary.** Your colleague owns *only* the provider adapter. I own orders, subscriptions, the ledger and HTTP routes. The adapter never touches the database; my code never speaks HTTP to QPay.

**What the colleague delivers:** `app/billing/payments/qpay.py` implementing the interface below; a sandbox contract-test run; the list of env vars. Endpoint paths and field names **must be verified against QPay's official merchant documentation or sandbox** (the third-party mirrors I consulted may be out of date).

**Interface (Python `Protocol`)**
```python
@dataclass(frozen=True)
class OrderRef:            # what Minuto gives the adapter
    order_id: str          # our UUID; used as QPay sender_invoice_no (idempotency key)
    amount_mnt: int        # integer tugrik, already validated server-side against the plans table
    description: str       # e.g. "Minuto Pro, 30 хоног"
    callback_url: str      # built by Minuto; contains the secret path token

@dataclass(frozen=True)
class InvoiceResult:       # what the adapter returns
    provider_invoice_id: str
    qr_text: str
    qr_image_b64: str | None
    deep_links: list[dict]           # [{name, description, logo, link}]
    expires_at: datetime | None

@dataclass(frozen=True)
class PaymentCheck:
    status: Literal["pending", "paid", "failed", "expired"]
    paid_amount_mnt: int | None
    paid_at: datetime | None
    provider_payment_id: str | None

class PaymentProvider(Protocol):
    name: str
    def create_invoice(self, order: OrderRef) -> InvoiceResult: ...   # idempotent per order_id
    def check_payment(self, provider_invoice_id: str) -> PaymentCheck: ...
    def cancel_invoice(self, provider_invoice_id: str) -> None: ...
    def parse_callback(self, query: Mapping[str, str], body: bytes) -> str | None: ...  # returns provider_invoice_id or None; **no trust**
```
**Errors:** `ProviderUnavailable` (timeout, 5xx, network; retryable) and `ProviderRejected` (4xx, bad credentials, invalid amount; not retryable). Never leak provider response bodies or credentials into exceptions or logs.
**Adapter obligations:** cache the access token and refresh before expiry; 10 s connect / 30 s read timeouts; bounded retry with backoff on `ProviderUnavailable`; thread-safe; all money as integer MNT; no `print`, no secret logging; unit-tested with a mocked HTTP layer.
**Environment variables** (the colleague defines; you enter the values in Render, never in chat or git): `QPAY_BASE_URL`, `QPAY_USERNAME`, `QPAY_PASSWORD`, `QPAY_INVOICE_CODE`, `QPAY_CALLBACK_SECRET`, plus a separate sandbox set.

**My side of the flow (Minuto)**
1. `POST /api/billing/checkout {plan}` → insert `payment_orders` (`pending`, amount from the plans table) → `create_invoice` → return QR/links.
2. `POST /api/billing/qpay/callback/{secret}` → constant-time secret check → `parse_callback` → **always** `check_payment` → only if `paid` **and** `paid_amount_mnt == order.amount_mnt` → `apply_paid_order(order_id)` (single transaction, idempotent via a unique order + the active-subscription index).
3. `GET /api/billing/orders/{id}` polls `check_payment` while `pending`.
4. Order states: `pending → paid | expired | failed | cancelled`; `paid` is terminal and immutable. Renewal extends from `max(now, current period_end)` so early renewal loses no days.
**Contract test suite** (mine, shared): the same tests run against a `FakeProvider` (CI) and against the QPay sandbox (manual): create is idempotent; check returns the expected statuses; a wrong amount is rejected; a duplicate callback does not double-apply; an unknown invoice is ignored; the callback body cannot cause a state change by itself.
**Recurring billing:** none assumed; manual 30-day renewal by default (your rule 2). The colleague should ask QPay in writing whether recurring/card-on-file exists for merchants and report back.

---
## H. Decisions I need from you before I implement Step 1
1. Approve the **Step 1 scope** (three tables, shadow only, default off, flag-controlled).
2. **B1 policy:** live recording may overdraft once; uploads are refused when they exceed what is left (or choose another combination).
3. **B4:** keep the debit when ASR succeeds but the summary fails? (recommended yes)
4. **C:** an editor processing in a workspace without Team is charged to the meeting owner. OK?
5. **Backends:** run the two Render-Shell checks in section A and record the outputs; decide whether the second backend and the old frontend stay or retire.
6. **Backups:** confirm a database backup exists/can be taken before the migration deploy.
7. Confirm the migration can be **merged and auto-deployed to both backends** in one approved window (because deploy = migration).
