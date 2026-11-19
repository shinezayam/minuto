# Document index

[MN](../README.md) · **EN**

> Minuto is an AI meeting assistant for Mongolian. These documents are **written from the code** (2026-11-19, commit `087b1f7`),
> not from plans or proposals. If the code and a document disagree, the code is right and the document gets fixed.

Audience: the internal team that builds and runs Minuto. The backend is this repo (`minuto`); the frontend is `minuto-frontend` (Next.js).

## Where to start?

| You are | Reading path |
|---|---|
| **New developer** | [Overview](overview.md) → [Architecture](architecture.md) → [API](api.md) |
| **Product / sales** | [Product](product.md) → [AI minutes](ai-minutes.md) → [Billing](../BILLING.md) |
| **Operator** | [Deployment](../DEPLOYMENT.md) → [Migrations](../MIGRATIONS.md) → [Billing](../BILLING.md) |

## Introduction

- **[Overview](overview.md)** · 4 min — What the repo is made of, how to run it, tests. `FastAPI` `Postgres` `Qwen`
- **[Product](product.md)** · 6 min — What Minuto is, who it is for, features, plans and pricing. `New`

## Platform

- **[Architecture](architecture.md)** · 8 min — Recording → transcript → minutes flow, durable job queue, audio storage. `Mermaid` `ASR` `Jobs`
- **[Identity and access](access.md)** · 5 min — Email/Google sign-in, JWT, WS tickets, workspace permission matrix, admins. `JWT` `RBAC`
- **[API reference](api.md)** · 6 min — Every endpoint by group, WebSocket protocol, error codes. `REST` `WebSocket` `OpenAPI`

## AI

- **[AI minutes](ai-minutes.md)** · 7 min — ASR/LLM providers, prompt structure, quote verification, human approval, Ask Minuto, eval. `Qwen` `Eval`
- **[AI quality guarantees](../AI_QUALITY.md)** · 2 min — What code enforces and what is prompt-only.
- **[Speaker diarization](../DIARIZATION.md)** · 3 min — Current limitation and options.

## Operations

- **[Deployment](../DEPLOYMENT.md)** · 5 min — Render/Vercel, env vars, health, smoke test, rollback. `Render`
- **[Migrations](../MIGRATIONS.md)** · 3 min — Alembic, backups, rollback. `Alembic`
- **[Billing](../BILLING.md)** · 5 min — Minute metering, plans, Wire (QPay) payments, rollout order. `Wire` `QPay` `MN`

## Other

- [`openapi.json`](../openapi.json) — committed API snapshot (`python -m app.openapi_snapshot`).
- [`../../eval/README.md`](../../eval/README.md) — minutes quality evaluation and model comparison.
