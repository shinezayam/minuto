# API reference

[MN](../api.md) · **EN**

> 6 min read · Source: `app/api/*.py` · Full schema: [`openapi.json`](../openapi.json), locally `http://localhost:8000/api/docs`

Every endpoint (except those marked public) needs `Authorization: Bearer <JWT>`. Error responses: `{"detail": "CODE: message"}`.

## 01 System
| Method | Path | |
|---|---|---|
| GET | `/healthz` | Liveness, public, no DB access |
| GET | `/api/health` | Public: `status, version, database, live_transcription` |
| GET | `/api/readyz` | Signed in: DB, disk, provider configuration (no paid calls) |

## 02 Auth — `/api/auth`
`POST /register` · `POST /login` · `GET /google/config` · `POST /google` · `GET /me` · `PATCH /me` · `POST /logout-all`

## 03 Meetings — `/api/meetings`

| Group | Endpoints |
|---|---|
| CRUD | `POST ""` · `GET ""` · `GET /{id}` · `PATCH /{id}` · `DELETE /{id}[?permanent=true]` (to trash on paid plans) |
| Recording | `POST /{id}/start` · `/pause` · `/resume` · `/end` · `POST /{id}/ws-ticket` |
| Audio | `POST /{id}/upload` (returns `audio_issue`) · `POST /{id}/audio-url` · `GET /{id}/audio` |
| Processing | `POST /{id}/process` (202) · `POST /{id}/regenerate-summary` (202) · `POST /{id}/topics` |
| Transcript | `GET`/`PUT /{id}/transcript` (`If-Match`) · `GET /{id}/transcript/revisions` · `POST …/revisions/{rid}/restore` · `POST /{id}/transcript/replace` |
| Minutes | `GET /{id}/minutes` · `PUT /{id}/minutes/status` · `PUT /{id}/minutes/{kind}/{index}/review` · `PUT /{id}/action-items` |
| Export | `GET /{id}/export.pdf` · `GET /{id}/export.docx` |
| List | `POST /{id}/archive` · `/unarchive` · `/pin` · `/unpin` |
| Trash | `GET /trash` · `DELETE /trash` · `POST /{id}/restore` |

## 04 WebSocket — `/ws/meetings/{id}/audio?ticket=…`

| Direction | Message |
|---|---|
| Client → | `{"type":"audio.chunk","sequence":N,"mime_type":"audio/webm","timestamp_ms":T}` followed by binary |
| ← Server | `audio.ack {sequence}` · `transcript.segment` / `transcript.error` (live transcript) · `recording.ready` · `error {code,message}` |

A repeated `sequence` overwrites and is acked again. Disallowed Origin → close `4403`.

## 05 Other

| Group | Endpoints |
|---|---|
| Ask | `POST /api/ask` — `{query, meeting_id?, history[]}` → `{answer, citations[]}` |
| Templates | `GET`/`POST /api/templates` · `GET`/`DELETE /api/templates/{id}` |
| Vocabulary | `GET`/`POST /api/vocabulary` · `DELETE /api/vocabulary/{id}` |
| Contacts | `GET /api/contacts` · `DELETE /api/contacts/{id}` |
| Workspaces | `GET`/`POST /api/workspaces` · `GET`/`DELETE /{wid}` · `GET /{wid}/members` · `POST /{wid}/invite` · `PATCH`/`DELETE /{wid}/members/{uid}` · `POST /{wid}/transfer` · `GET`/`POST /{wid}/projects` · `DELETE /{wid}/projects/{pid}` |
| Billing | `GET /api/billing/plans` (public) · `GET /api/billing/me` · `POST /api/billing/checkout` · `GET /api/billing/orders[/{id}]` · `POST /api/billing/cancel` · `POST /api/billing/webhooks/wire` |
| Feedback, requests | `POST /api/feedback` · `POST /api/contact-requests` (public) |
| Admin | `GET /api/admin/me` · `GET`/`POST /api/admin/admins` · `DELETE /api/admin/admins/{email}` · `GET /api/admin/contact-requests` · `PATCH …/{id}` |

## 06 Common error codes
| HTTP | Meaning |
|---|---|
| 401 | Missing / invalid token |
| 402 | Out of minutes (billing `enforce`) |
| 403 | `FORBIDDEN` — can view the meeting but not perform this action |
| 404 | Not found, or no access |
| 409 | `ALREADY_PROCESSING` — processing in progress; `ARCHIVED` |
| 412 | `CONFLICT` — `If-Match` mismatch (changed elsewhere) |
| 413 / 415 / 422 | Too large / unsupported format / invalid data |
| 429 | Rate limited |

## 07 Contract
`docs/openapi.json` is a committed snapshot. After an API change run `python -m app.openapi_snapshot`, otherwise `tests/test_ops.py` fails.
