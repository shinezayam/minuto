# Deployment

Backend: Render (FastAPI). Frontend: Vercel (Next.js). Data: Postgres (Render) and, later, Supabase Storage (task 2.2).

## Topology and hard constraints
* **Single instance only.** Live transcription state, the WS one-time ticket store and the in-memory rate
  limiter live in process memory. Do not scale out to >1 instance/worker until they move to a shared store.
* **Render free tier is not suitable for a pilot**: the disk is wiped on every deploy/restart (recordings are
  lost), the service sleeps, 512 MB RAM, and the free Postgres expires after 30 days.
* The audio files currently live on the local disk (`AUDIO_STORAGE_PATH`). Until task 2.2 ships, use a Render
  persistent disk or accept loss on deploy.

## Environment variables (backend)
| Variable | Required in prod | Default | Notes |
|---|---|---|---|
| `ENVIRONMENT` | yes (`production`) | `development` | Disables `/api/docs`, requires `JWT_SECRET`, trusts the proxy's `X-Forwarded-For`. `RENDER` env also counts. |
| `JWT_SECRET` | **yes** | – | App refuses to start in production without it. Render: `generateValue: true`. |
| `DATABASE_URL` | **yes** | SQLite file | Postgres. In production SQLite logs an ERROR (data lost on deploy). |
| `CORS_ORIGINS` | yes | `http://localhost:3000` | Comma list of frontend origins. Also the WebSocket Origin allow-list unless `WS_ALLOWED_ORIGINS` is set. |
| `QWEN_API_KEY` | yes (if providers ≠ demo) | – | Secret, set in the dashboard, never in git. |
| `ASR_PROVIDER` / `LLM_PROVIDER` | yes | `demo` | `omni`/`whisper` and `qwen`. |
| `ALLOWED_HOSTS` | recommended | `*` | e.g. `minuto-api.onrender.com`. |
| `ALLOW_QUERY_TOKEN` | – | `true` | Deprecation window for `?token=` on audio/export + WS. Set `false` once old frontends are gone. |
| `MAX_UPLOAD_BYTES` | – | 300 MB | Single upload. |
| `MAX_RECORDING_SEC` | – | 14400 | Upload and live recording length. |
| `MAX_WS_TOTAL_BYTES` | – | 400 MB | Live recording total. |
| `USER_DAILY_UPLOAD_BYTES` / `USER_TOTAL_STORAGE_BYTES` | – | 2 GB / 10 GB | Per-user quotas. |
| `MIN_FREE_DISK_BYTES` | – | 500 MB | New audio refused below this. |
| `RATE_LIMIT_PER_MIN` / `RATE_LIMIT_AUTH_PER_MIN` / `RATE_LIMIT_EXPENSIVE_PER_MIN` | – | 120 / 10 / 20 | Per IP, login+register per IP, AI/processing per user. |
| `WS_TICKET_TTL_SEC` / `AUDIO_URL_TTL_SEC` | – | 60 / 900 | |

Frontend (Vercel): `NEXT_PUBLIC_API_BASE_URL` = the backend URL (build-time).

## Health and logs
* `GET /healthz` – liveness, public, no data.
* `GET /api/health` – public, minimal (`status, version, database, live_transcription`).
* `GET /api/readyz` – authenticated: DB, storage (writable + free space), provider configuration. Makes **no**
  paid calls.
* Every response carries `X-Request-ID`. One JSON access-log line per request on logger `minuto.access`
  (method, path, status, duration, ip, request id). **Never logged:** query strings (they can hold tickets),
  headers, bodies, transcripts. Start uvicorn with `--no-access-log` (already set in `render.yaml` and the
  Dockerfile), otherwise uvicorn's own access log prints query strings.

## Deploy / rollback
1. CI green (backend: migrations, OpenAPI snapshot, tests; frontend: typecheck, i18n, build).
2. **Back up the database first** (`pg_dump … -Fc`, see `MIGRATIONS.md`) when the release contains a migration.
3. Backend first (it accepts old and new clients), then the frontend.
4. Check `/healthz`, then sign in and open `/api/readyz`.
5. Rollback: redeploy the previous commit. If a migration ran and is reversible,
   `python -m app.migrate downgrade <rev>`; otherwise restore the dump.

## Contract between frontend and backend
`docs/openapi.json` is a committed snapshot. `tests/test_ops.py` fails when the API changes without updating it
(`python -m app.openapi_snapshot`), so a contract change is always a deliberate, reviewed diff.

## Not done yet (see PRODUCTION_READINESS_TODO.md)
Staging environment + smoke script, error tracking (Sentry/Render alerts), shared store for multi-instance,
persistent audio storage (2.2), durable jobs (2.1).
