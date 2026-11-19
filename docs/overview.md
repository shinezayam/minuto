# Тойм

**MN** · [EN](en/overview.md)

> 4 мин унших · Эх сурвалж: `app/`, `requirements.txt`, `render.yaml`, `.env.example`

Minuto бол **монгол хэл дээрх хурлыг бичиж, бичвэр болгож, албан ёсны хурлын тэмдэглэл гаргадаг** AI туслах.
Энэ репо нь зөвхөн backend (API + ард ажиллах боловсруулалт). Хэрэглэгчийн интерфейс `minuto-frontend` репод.

| 10 | 12 | 2 | 32 |
|:-:|:-:|:-:|:-:|
| API router | DB migration | AI provider (ASR, LLM) | тестийн модуль |

## 01 Юунаас бүтдэг

| Хэсэг | Технологи | Юу хийдэг |
|---|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2, Alembic | REST + WebSocket API, эрх, billing |
| Мэдээллийн сан | Postgres (prod), SQLite (локал) | Хурал, бичвэр, тэмдэглэл, багц, ажлын дараалал |
| Ярианаас текст (ASR) | Qwen-Omni (`qwen3.8-omni-flash`), эсвэл локал Whisper | Аудиог 30 сек хэсгүүдээр текст болгоно |
| Хурлын тэмдэглэл (LLM) | Qwen (`qwen3.8-max`), Гэрэгэ Token Hub-аар | Хураангуй, шийдвэр, даалгавар, асуулт |
| Аудио | ffmpeg (`imageio-ffmpeg`), локал диск + Supabase Storage | Хөрвүүлэх, хэсэглэх, байнгын хадгалалт |
| Экспорт | `python-docx`, `fpdf2` | DOCX (бичвэртэй), PDF (албан A4 хэлбэр) |
| Төлбөр | Wire (`wire.mn`) → QPay | Багц худалдах, webhook |
| Frontend | Next.js 16, React 19, Tailwind | `minuto-frontend` репо, Vercel |
| Hosting | Render (backend), Vercel (frontend) | |

## 02 Репогийн бүтэц

```
minuto/
├── app/
│   ├── main.py            # FastAPI app, middleware, lifespan (migration, job worker, billing цикл)
│   ├── config.py          # Бүх тохиргоо (env → Settings)
│   ├── db.py              # SQLAlchemy модель — схемийн эх
│   ├── security.py        # Rate limit, security header, request log
│   ├── migrate.py         # python -m app.migrate current|upgrade|downgrade
│   ├── api/               # HTTP/WS route-ууд (meetings, auth, billing, workspaces, ask …)
│   ├── services/          # Бизнес логик (asr, llm, processing, jobs, billing, export …)
│   ├── models/            # Pydantic схем (minutes.py = тэмдэглэлийн бүтэц)
│   ├── migrations/        # Alembic revision 0001–0012
│   └── assets/fonts/      # PDF-ийн кирилл фонт
├── eval/                  # Тэмдэглэлийн чанарын үнэлгээ, загвар харьцуулах
├── scripts/smoke.py       # Deploy-ийн дараах end-to-end шалгалт
├── tests/                 # pytest (offline, demo provider)
├── docs/                  # Энэ баримтууд
├── ci/                    # GitHub Actions workflow (гараар .github/ руу хуулна)
├── Dockerfile · render.yaml (лавлагаа) · .env.example
```

## 03 Ажиллуулах

### Урьдчилсан шаардлага
- Python 3.11, ffmpeg (`PATH`-д эсвэл `FFMPEG_PATH`; байхгүй бол `imageio-ffmpeg`-ийнхийг ашиглана)
- Бодит AI хэрэгтэй бол `QWEN_API_KEY`. Үгүй бол `ASR_PROVIDER=demo`, `LLM_PROVIDER=demo` — үнэгүй, `[ДЕМО]` шошготой үр дүн.

### Гараар
```bash
python -m venv .venv && .venv\Scripts\activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                  # бөглөнө; .env-г git-д хэзээ ч оруулахгүй
uvicorn app.main:app --reload --port 8000
```
Эхлэхэд migration автоматаар `head` хүртэл ажиллана. `DATABASE_URL` хоосон бол `./minuto.db` (SQLite).
API баримт: `http://localhost:8000/api/docs` (production дээр унтраалттай).

### Docker
```bash
docker build -t minuto . && docker run -p 8000:8000 --env-file .env minuto
```

> ⚠ `ENVIRONMENT=production` үед `JWT_SECRET`-гүй бол сервер **эхлэхгүй**. Production-д SQLite ашиглавал ERROR лог гарна
> (deploy бүрт өгөгдөл арилна).

## 04 Тест
```bash
pip install -r requirements-dev.txt
pytest                         # бүгд offline, demo provider, SQLite — мөнгө зарахгүй
python scripts/smoke.py <url>  # амьд сервер дээр бүртгэл → upload → боловсруулалт → экспорт → устгал
```
CI: migration, OpenAPI snapshot (`docs/openapi.json` өөрчлөгдсөн бол унана), тестүүд.

## 05 Хувь нэмэр
- Схем өөрчлөх бол `app/db.py` + шинэ Alembic revision (→ [Migration](MIGRATIONS.md)).
- API өөрчилбөл `python -m app.openapi_snapshot` ажиллуулж snapshot-ийг commit хийнэ.
- Prompt, загвар солих бол өмнө/дараах `eval` тоог гаргана (→ [AI тэмдэглэл](ai-minutes.md#06-чанарын-үнэлгээ-eval)).

Production тохиргоо энд байхгүй — [Deployment](DEPLOYMENT.md)-ийг үз.
