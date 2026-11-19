# Identity and access

[MN](../access.md) · **EN**

> 5 min read · Source: `app/services/{auth,access,admins,workspaces}.py`, `app/security.py`

## 01 Sign-in

| Method | Endpoint | Notes |
|---|---|---|
| Email + password | `POST /api/auth/register`, `/login` | PBKDF2-SHA256. Name derived from the email if not given. `ALLOW_REGISTRATION` |
| Google | `GET /api/auth/google/config`, `POST /api/auth/google` | Verifies the GIS ID token; links by `google_sub` or by matching email |
| Sign out everywhere | `POST /api/auth/logout-all` | `token_version`++ → every old token is revoked |

JWT (`JWT_SECRET`, `JWT_TTL_DAYS=30`) in the `Authorization: Bearer` header.

**Where headers cannot be sent:**
- WebSocket → `POST /meetings/{id}/ws-ticket`: a one-time ticket valid for 60 s.
- `<audio>` playback → `POST /meetings/{id}/audio-url`: a signed URL valid for 15 min.
- `?token=` remains for old clients (`ALLOW_QUERY_TOKEN`); the current frontend does not use it.

## 02 Meeting permissions (`access.py` — the single source)

**Personal meeting** (`workspace_id = NULL`): owner only.
**Workspace meeting**: decided by the member's role.

| Action | owner | admin | editor | viewer |
|---|:-:|:-:|:-:|:-:|
| View (meeting, transcript, audio, export) | ✓ | ✓ | ✓ | ✓ |
| Create meeting | ✓ | ✓ | ✓ | – |
| Edit (transcript, minutes, process) | ✓ | ✓ | ✓ | – |
| Delete / archive | ✓ | ✓ | own only | – |
| Approve minutes | ✓ | ✓ | – | – |

- Non-member → **404** (the meeting's existence is not revealed). Member without the right → **403**.
- Responses include `my_role` and `created_by_me` so the frontend can show or hide buttons.

## 03 Workspaces

- Every user automatically has a **personal** workspace and can create **organisation** workspaces.
- Invite members (`/invite`), change roles, remove members, transfer ownership (`/transfer`), delete.
- Projects group meetings inside a workspace.
- A Team plan belongs to a workspace: its meetings use the workspace's minutes.

## 04 Admins (`admins.py`, migration 0010)

| Level | Who | Rights |
|---|---|---|
| Owner admin | `ADMIN_EMAILS` env | Everything + add/remove other admins |
| Added admin | `admin_grants` table | Enterprise requests, feedback, sandbox checkout |

Endpoints: `/api/admin/me`, `/admins`, `/contact-requests`.

## 05 Request-level protection
- `TrustedHostMiddleware` (`ALLOWED_HOSTS`), CORS (`CORS_ORIGINS`), WS Origin check.
- Security headers, per-IP rate limiting (in production the proxy's `X-Forwarded-For` is trusted).
- `/api/docs` and `/api/openapi.json` are disabled in production.
- Error details go to the server log only; clients receive `CODE: message`.
