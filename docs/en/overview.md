# Overview

[MN](../overview.md) · **EN**

> 4 min read · Source: `app/`, `requirements.txt`, `render.yaml`, `.env.example`

Minuto is an AI assistant that **records Mongolian-language meetings, transcribes them and produces formal meeting minutes**.
This repo is the backend only (API + background processing). The user interface lives in the `minuto-frontend` repo.

| 10 | 12 | 2 | 32 |
|:-:|:-:|:-:|:-:|
| API routers | DB migrations | AI providers (ASR, LLM) | test modules |

## 01 What it is made of

| Part | Technology | What it does |
|---|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2, Alembic | REST + WebSocket API, permissions, billing |
| Database | Postgres (prod), SQLite (local) | Meetings, transcripts, minutes, plans, job queue |
| Speech-to-text (ASR) | Qwen-Omni (`qwen3.8-omni-flash`), or local Whisper | Transcribes audio in 30 s chunks |
| Minutes (LLM) | Qwen (`qwen3.8-max`) via Gerege Token Hub | Summary, decisions, action items, questions |
| Audio | ffmpeg (`imageio-ffmpeg`), local disk + Supabase Storage | Conversion, chunking, durable storage |
| Export | `python-docx`, `fpdf2` | DOCX (with transcript), PDF (official A4 format) |
| Payments | Wire (`wire.mn`) → QPay | Plan purchase, webhooks |
| Frontend | Next.js 16, React 19, Tailwind | `minuto-frontend` repo, Vercel |
| Hosting | Render (backend), Vercel (frontend) | |

## 02 Repo structure

```
minuto/
├── app/
│   ├── main.py            # FastAPI app, middleware, lifespan (migrations, job workers, billing loop)
│   ├── config.py          # All settings (env → Settings)
│   ├── db.py              # SQLAlchemy models — source of the schema
│   ├── security.py        # Rate limiting, security headers, request log
│   ├── migrate.py         # python -m app.migrate current|upgrade|downgrade
│   ├── api/               # HTTP/WS routes (meetings, auth, billing, workspaces, ask …)
│   ├── services/          # Business logic (asr, llm, processing, jobs, billing, export …)
│   ├── models/            # Pydantic schemas (minutes.py = minutes structure)
│   ├── migrations/        # Alembic revisions 0001–0012
│   └── assets/fonts/      # Cyrillic fonts for PDF
├── eval/                  # Minutes quality evaluation, model comparison
├── scripts/smoke.py       # Post-deploy end-to-end check
├── tests/                 # pytest (offline, demo providers)
├── docs/                  # These documents (MN; English in docs/en/)
├── ci/                    # GitHub Actions workflow (copy to .github/ by hand)
├── Dockerfile · render.yaml (reference) · .env.example
```

## 03 Running it

### Prerequisites
- Python 3.11, ffmpeg (on `PATH` or `FFMPEG_PATH`; otherwise the one from `imageio-ffmpeg` is used)
- For real AI, a `QWEN_API_KEY`. Otherwise `ASR_PROVIDER=demo`, `LLM_PROVIDER=demo` — free, output labelled `[ДЕМО]`.

### Manually
```bash
python -m venv .venv && .venv\Scripts\activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                  # fill it in; never commit .env
uvicorn app.main:app --reload --port 8000
```
Migrations run to `head` automatically on startup. With an empty `DATABASE_URL` the app uses `./minuto.db` (SQLite).
API docs: `http://localhost:8000/api/docs` (disabled in production).

### Docker
```bash
docker build -t minuto . && docker run -p 8000:8000 --env-file .env minuto
```

> ⚠ With `ENVIRONMENT=production` the server **refuses to start** without `JWT_SECRET`. Using SQLite in production logs an
> ERROR (data is lost on every deploy).

## 04 Tests
```bash
pip install -r requirements-dev.txt
pytest                         # all offline, demo providers, SQLite — costs nothing
python scripts/smoke.py <url>  # on a live server: register → upload → process → export → delete
```
CI: migrations, OpenAPI snapshot (fails if `docs/openapi.json` is stale), tests.

## 05 Contributing
- Schema changes: `app/db.py` + a new Alembic revision (→ [Migrations](../MIGRATIONS.md)).
- API changes: run `python -m app.openapi_snapshot` and commit the snapshot.
- Prompt or model changes: produce before/after `eval` numbers (→ [AI minutes](ai-minutes.md#06-quality-evaluation-eval)).

Production configuration is not here — see [Deployment](../DEPLOYMENT.md).
