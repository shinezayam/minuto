# Баримтын индекс

**MN** · [EN](en/README.md)

> Minuto — монгол хэл дээрх хурлын AI туслах. Энэ баримтууд **кодоос уншиж бичсэн** (2026-11-19, commit `087b1f7`),
> төлөвлөгөө, санал биш. Кодтой зөрвөл код зөв, баримтыг засна.

Хэнд: Minuto-г хөгжүүлж, ажиллуулж буй дотоод баг. Backend энэ репо (`minuto`), frontend нь `minuto-frontend` (Next.js).

## Хаанаас эхлэх вэ?

| Та хэн бэ | Унших дараалал |
|---|---|
| **Шинэ хөгжүүлэгч** | [Тойм](overview.md) → [Архитектур](architecture.md) → [API](api.md) |
| **Бүтээгдэхүүн / борлуулалт** | [Бүтээгдэхүүн](product.md) → [AI тэмдэглэл](ai-minutes.md) → [Billing](BILLING.md) |
| **Оператор** | [Deployment](DEPLOYMENT.md) → [Migration](MIGRATIONS.md) → [Billing](BILLING.md) |

## Танилцуулга

- **[Тойм](overview.md)** · 4 мин — Репо юунаас бүтдэг, хэрхэн ажиллуулах, тест. `FastAPI` `Postgres` `Qwen`
- **[Бүтээгдэхүүн](product.md)** · 6 мин — Minuto гэж юу вэ, хэнд зориулсан, ямар боломжтой, багц ба үнэ. `Шинэ`

## Платформ

- **[Архитектур](architecture.md)** · 8 мин — Бичлэг → бичвэр → хурлын тэмдэглэл урсгал, durable ажлын дараалал, аудио хадгалалт. `Mermaid` `ASR` `Jobs`
- **[Нэвтрэлт ба эрх](access.md)** · 5 мин — Имэйл/Google нэвтрэлт, JWT, WS ticket, workspace-ийн эрхийн матриц, админ. `JWT` `RBAC`
- **[API лавлах](api.md)** · 6 мин — Бүх endpoint бүлгээр, WebSocket протокол, алдааны код. `REST` `WebSocket` `OpenAPI`

## AI

- **[AI тэмдэглэл](ai-minutes.md)** · 7 мин — ASR/LLM provider, prompt-ийн бүтэц, ишлэлийн шалгалт, хүний батлалт, Ask Minuto, eval. `Qwen` `Eval`
- **[AI чанарын баталгаа](AI_QUALITY.md)** · 2 мин — Юу кодоор баталгаажсан, юу нь зөвхөн prompt. `EN`
- **[Яригч ялгах (diarization)](DIARIZATION.md)** · 3 мин — Одоогийн хязгаарлалт ба сонголтууд. `EN`

## Ажиллагаа

- **[Deployment](DEPLOYMENT.md)** · 5 мин — Render/Vercel, env хувьсагч, health, smoke тест, rollback. `Render` `EN`
- **[Migration](MIGRATIONS.md)** · 3 мин — Alembic, backup, буцаах. `Alembic` `EN`
- **[Billing](BILLING.md)** · 5 мин — Минут тоолох, багц, Wire (QPay) төлбөр, нэвтрүүлэх дараалал. `Wire` `QPay`

## Бусад

- [`openapi.json`](openapi.json) — API-ийн commit-лэгдсэн snapshot (`python -m app.openapi_snapshot`).
- [`../eval/README.md`](../eval/README.md) — Тэмдэглэлийн чанарын үнэлгээ, загвар харьцуулах.
