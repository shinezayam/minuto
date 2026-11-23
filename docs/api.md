# API лавлах

**MN** · [EN](en/api.md)

> 6 мин унших · Эх сурвалж: `app/api/*.py` · Бүрэн схем: [`openapi.json`](openapi.json), локал `http://localhost:8000/api/docs`

Бүх endpoint (нээлттэй гэснээс бусад) `Authorization: Bearer <JWT>` шаардана. Алдааны хариу: `{"detail": "CODE: мессеж"}`.

## 01 Систем
| Method | Path | |
|---|---|---|
| GET | `/healthz` | Liveness, нээлттэй, DB-д хандахгүй |
| GET | `/api/health` | Нээлттэй: `status, version, database, live_transcription` |
| GET | `/api/readyz` | Нэвтэрсэн: DB, диск, provider тохиргоо (төлбөртэй дуудлагагүй) |

## 02 Нэвтрэлт — `/api/auth`
`POST /register` · `POST /login` · `GET /google/config` · `POST /google` · `GET /me` · `PATCH /me` · `POST /logout-all`

## 03 Хурал — `/api/meetings`

| Бүлэг | Endpoint |
|---|---|
| CRUD | `POST ""` · `GET ""` · `GET /{id}` · `PATCH /{id}` · `DELETE /{id}[?permanent=true]` (төлбөртэй багцад хогийн сав руу) |
| Бичлэг | `POST /{id}/start` · `/pause` · `/resume` · `/end` · `POST /{id}/ws-ticket` |
| Аудио | `POST /{id}/upload` (`audio_issue` буцаана) · `POST /{id}/audio-url` · `GET /{id}/audio` |
| Боловсруулалт | `POST /{id}/process` (202) · `POST /{id}/regenerate-summary` (202) · `POST /{id}/topics` |
| Бичвэр | `GET`/`PUT /{id}/transcript` (`If-Match`) · `GET /{id}/transcript/revisions` · `POST …/revisions/{rid}/restore` · `POST /{id}/transcript/replace` |
| Тэмдэглэл | `GET /{id}/minutes` · `PUT /{id}/minutes/status` · `PUT /{id}/minutes/{kind}/{index}/review` · `PUT /{id}/action-items` |
| Экспорт | `GET /{id}/export.pdf` · `GET /{id}/export.docx` |
| Жагсаалт | `POST /{id}/archive` · `/unarchive` · `/pin` · `/unpin` |
| Хогийн сав | `GET /trash` · `DELETE /trash` · `POST /{id}/restore` |

## 04 WebSocket — `/ws/meetings/{id}/audio?ticket=…`

| Чиглэл | Мессеж |
|---|---|
| Клиент → | `{"type":"audio.chunk","sequence":N,"mime_type":"audio/webm","timestamp_ms":T}` дараа нь binary |
| ← Сервер | `audio.ack {sequence}` · `transcript.segment` / `transcript.error` (шууд бичвэр) · `recording.ready` · `error {code,message}` |

Ижил `sequence` дахин ирвэл дарж бичээд дахин ack. Origin зөвшөөрөгдөөгүй бол close `4403`.

## 05 Бусад

| Бүлэг | Endpoint |
|---|---|
| Ask | `POST /api/ask` — `{query, meeting_id?, history[]}` → `{answer, citations[]}` |
| Загвар | `GET`/`POST /api/templates` · `GET`/`DELETE /api/templates/{id}` |
| Толь бичиг | `GET`/`POST /api/vocabulary` · `DELETE /api/vocabulary/{id}` |
| Оролцогчид | `GET /api/contacts` · `DELETE /api/contacts/{id}` |
| Workspace | `GET`/`POST /api/workspaces` · `GET`/`DELETE /{wid}` · `GET /{wid}/members` · `POST /{wid}/invite` · `PATCH`/`DELETE /{wid}/members/{uid}` · `POST /{wid}/transfer` · `GET`/`POST /{wid}/projects` · `DELETE /{wid}/projects/{pid}` |
| Billing | `GET /api/billing/plans` (нээлттэй) · `GET /api/billing/me` · `POST /api/billing/checkout` · `GET /api/billing/orders[/{id}]` · `POST /api/billing/cancel` · `POST /api/billing/webhooks/wire` |
| Санал, хүсэлт | `POST /api/feedback` · `POST /api/contact-requests` (нээлттэй) |
| Админ | `GET /api/admin/me` · `GET`/`POST /api/admin/admins` · `DELETE /api/admin/admins/{email}` · `GET /api/admin/contact-requests` · `PATCH …/{id}` |

## 06 Түгээмэл алдааны код
| HTTP | Утга |
|---|---|
| 401 | Токен алга / хүчингүй |
| 402 | Минут дууссан (billing `enforce`) |
| 403 | `FORBIDDEN` — хурлыг харж болно, үйлдэл хийх эрхгүй |
| 404 | Олдсонгүй, эсвэл хандах эрхгүй |
| 409 | `ALREADY_PROCESSING` — идэвхтэй боловсруулалт байна; `ARCHIVED` |
| 412 | `CONFLICT` — `If-Match` зөрсөн (өөр газраас өөрчилсөн) |
| 413 / 415 / 422 | Хэт том / дэмжихгүй формат / буруу өгөгдөл |
| 429 | Rate limit |

## 07 Гэрээ
`docs/openapi.json` commit-лэгдсэн snapshot. API өөрчилбөл `python -m app.openapi_snapshot` — эс бөгөөс `tests/test_ops.py` унана.
