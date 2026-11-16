> **Handoff note:** earlier planning document. Where it differs from `BILLING_HANDOFF.md` and `BILLING_STEP1_PROPOSAL.md` (which carry the agreed billing rules and the Step 1 design), those two win.

# Minuto billing: plan for approval (nothing built, nothing deployed)

Date: 2026-11-16. Decisions below are yours (from your table). Items marked **NEEDS DECISION** change what I build. QPay endpoint details come from community mirrors, not QPay's own portal, so every endpoint is **verified against official QPay docs or your QPay contact before coding**.

## 1. Your decisions (taken as given)
| Topic | Decision |
|---|---|
| Payment | QPay |
| Free | One-time 30 minutes |
| Starter / Pro / Team | Monthly renewing minutes |
| Minutes run out | No new transcription starts; existing meetings stay viewable |
| Unused minutes | Do not roll over |
| Upgrade | Allowed; price-difference rule decided later |
| Cancel | Use the plan until the paid period ends |
| Refunds | Separate approved terms (not in the product until written) |
| Enterprise | Send a quote request |

## 2. One technical fact that shapes "monthly renewing"
QPay, as far as I could find, is **invoice-based, not subscription-based**: I found no recurring or card-on-file billing in its API docs. So each month the customer **pays a new invoice** (QR / bank app), and Minuto cannot charge them automatically. Consequences:
- A paid period = 30 days from the payment time (proposed), with an in-app "renew" button, plus reminders before it ends.
- "Cancel" simply means *do not renew*; access continues to the end of the paid period. No chargeback or auto-charge logic needed.
- If QPay offers a recurring product to your merchant account, we can revisit; confirm with them.

## 3. What a "minute" is **NEEDS DECISION (my proposal in bold)**
1. **Unit:** length of the audio that is transcribed, in whole minutes rounded **up** per meeting. Live recordings and uploaded files both count.
2. **When charged:** reserve at the start of processing from the audio duration, **debit on success, release on failure**. A failed run costs nothing.
3. **Re-processing the same audio:** **free** (otherwise a bad transcript punishes the user); counts again only if the audio changes.
4. **Live transcription while recording:** counts as the recording's duration (same meeting, charged once).
5. **Team plan (900 min):** **per workspace**, shared by its members (a Team plan that one person owns is hard to justify otherwise). Starter and Pro are per account. Needs your yes/no.
6. **Existing users at launch:** **each existing account gets the one-time 30 free minutes**, and no meeting is ever hidden or deleted. Without this, today's users would hit zero on day one.
7. **When a paid period lapses unrenewed:** back to "no minutes" (the Free 30 are one-time and already granted). Viewing, exports and DOCX keep working.

## 4. Architecture

**Database (additive Alembic migration `0005`, no existing table altered destructively)**
- `plans` (code, name, monthly_minutes, price_mnt, active) — seeded with exactly your five rows.
- `subscriptions` (id, owner_user_id or workspace_id, plan_code, status: active/expired, period_start, period_end, cancel_at_period_end).
- `minute_ledger` (id, owner, meeting_id, kind: grant/reserve/debit/release/period_reset, minutes, created_at) — append-only, so every balance is explainable.
- `payment_orders` (id, owner, plan_code, amount_mnt, status: pending/paid/expired/failed, qpay_invoice_id, sender_invoice_no unique, created_at, paid_at, raw_check_result).
- `contact_requests` (id, name, email, company, message, created_at, status).

**Backend endpoints**
- `GET /api/billing/plans`, `GET /api/billing/me` (plan, minutes left, period end).
- `POST /api/billing/checkout` {plan} → creates a `payment_orders` row and a QPay invoice, returns QR + deep links.
- `GET /api/billing/orders/{id}` → status (polls QPay's payment check if still pending).
- `POST /api/billing/qpay/callback/{secret}` → **never trusted by itself**: it only triggers a server-side `payment/check` for that invoice; the order is marked paid only if QPay confirms and the amount matches. Idempotent.
- `POST /api/billing/cancel` → sets `cancel_at_period_end`.
- `POST /api/contact-requests` → stores the Enterprise request (rate-limited, validated).
- Enforcement hook in `processing.start` and the live-recording start: `402 MINUTES_EXHAUSTED` with a Mongolian message and a link to pricing.

**Security and money safety**
- Amounts and plan prices come from the server table, never the client.
- Credentials (`QPAY_USERNAME`, `QPAY_PASSWORD`, `QPAY_INVOICE_CODE`, callback secret) live only in Render env vars that **you** enter; I never see them.
- Callback is secret-in-path and verified by a server-side payment check; duplicate callbacks cannot double-grant (unique order + ledger idempotency).
- Rate limits on checkout and contact; no card data ever touches Minuto.
- Sandbox first; production QPay only after your approval.

**Frontend**
- Pricing buttons become real: Free → register; paid → `/checkout?plan=…` (login required); Enterprise → contact form modal/page.
- Checkout page: plan summary, QPay QR + bank-app links, "waiting for payment" with polling, success/expired states.
- Billing area in Settings: current plan, minutes used/left, period end, renew, cancel at period end, order history.
- A minutes meter in the dashboard and clear "minutes exhausted" state when starting a meeting.
- All copy in Mongolian with English dictionary entries; no invented policy text (refund/upgrade-difference text stays out until you approve wording).

**Contact (Enterprise)**
- Stored in the database. Notification email needs a mail provider (SMTP or similar) and a destination address from you; until then I add a simple admin list view or a notification to the address you give me via your chosen provider.

## 5. Rollout (each step is a PR you approve; production touched only where noted)
| Step | Content | Production effect |
|---|---|---|
| B0 | You approve §3 and give the items in §6 | none |
| B1 | Schema `0005`, plan seed, ledger, metering in **shadow mode** (records usage, blocks nothing), full tests | one additive migration on deploy |
| B2 | QPay client + checkout/callback against **sandbox**, with a faked QPay in tests | none until sandbox env vars set |
| B3 | Frontend: checkout, billing page, meter, contact form (buttons stay disabled in production behind a flag) | UI deploy only |
| B4 | End-to-end sandbox payment test with a synthetic account | none |
| B5 | Enable enforcement + production QPay credentials (**your action**), grant existing users 30 minutes first | the real switch; reversible by flag |
| B6 | Monitor a week: failed payments, unmatched callbacks, ledger totals | – |

## 6. What I need from you
1. **QPay:** a merchant account with API access (username, password, invoice code) and the sandbox credentials. You enter secrets into Render; never paste them to me.
2. **QPay recurring:** ask QPay whether they offer recurring/card-on-file for merchants; otherwise the manual monthly invoice above stands.
3. **Enterprise contact:** the email (or inbox) that receives quote requests, and whether you have an email provider.
4. **§3 items 1–7:** reply "yes" or change any (especially 3, 5, 6 and 7).
5. **Upgrade price difference** and **refund terms:** deferred as you said; the UI will not mention them until wording is approved.
6. **Domain/URL:** which public backend URL QPay should call back (the main service `minuto-ai-meeting-assistant.onrender.com`; confirm the second backend is not used).

## 7. Risks I am flagging now
- Enforcement can lock out users: hence shadow mode, the existing-user grant and the feature flag.
- Metering relies on correct audio duration; I'll test with real ffmpeg-generated files, including failures and retries.
- Two live backends exist; billing must run on exactly one.
- Local disk and processing issues from the earlier audit are separate; billing does not fix them.
