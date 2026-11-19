# Нэвтрэлт ба эрх

**MN** · [EN](en/access.md)

> 5 мин унших · Эх сурвалж: `app/services/{auth,access,admins,workspaces}.py`, `app/security.py`

## 01 Нэвтрэлт

| Арга | Endpoint | Тайлбар |
|---|---|---|
| Имэйл + нууц үг | `POST /api/auth/register`, `/login` | PBKDF2-SHA256. Нэр өгөөгүй бол имэйлээс гаргана. `ALLOW_REGISTRATION` |
| Google | `GET /api/auth/google/config`, `POST /api/auth/google` | GIS ID token шалгана; `google_sub`-аар, эсвэл ижил имэйлээр холбоно |
| Бүгдээс гарах | `POST /api/auth/logout-all` | `token_version`++ → бүх хуучин токен хүчингүй |

JWT (`JWT_SECRET`, `JWT_TTL_DAYS=30`) `Authorization: Bearer` header-ээр.

**Header явуулж чаддаггүй газар:**
- WebSocket → `POST /meetings/{id}/ws-ticket`: 60 сек, нэг удаагийн ticket.
- `<audio>` тоглуулах → `POST /meetings/{id}/audio-url`: 15 мин настай гарын үсэгтэй URL.
- `?token=` хуучин клиентэд үлдсэн (`ALLOW_QUERY_TOKEN`), шинэ frontend ашиглахгүй.

## 02 Хурлын эрх (`access.py` — цорын ганц эх)

**Хувийн хурал** (`workspace_id = NULL`): зөвхөн эзэн.
**Workspace-ийн хурал**: гишүүний үүргээр.

| Үйлдэл | owner | admin | editor | viewer |
|---|:-:|:-:|:-:|:-:|
| Харах (хурал, бичвэр, аудио, экспорт) | ✓ | ✓ | ✓ | ✓ |
| Хурал үүсгэх | ✓ | ✓ | ✓ | – |
| Засах (бичвэр, тэмдэглэл, боловсруулах) | ✓ | ✓ | ✓ | – |
| Устгах / архивлах | ✓ | ✓ | өөрийнхөө | – |
| Тэмдэглэл батлах | ✓ | ✓ | – | – |

- Гишүүн биш → **404** (хурал байгааг ч мэдэгдэхгүй). Гишүүн боловч эрхгүй → **403**.
- Хариунд `my_role`, `created_by_me` ирнэ — frontend товч нуух/харуулахад.

## 03 Workspace

- Хэрэглэгч бүр автоматаар **хувийн** workspace-тэй; **байгууллагын** workspace үүсгэж болно.
- Гишүүн урих (`/invite`), үүрэг солих, хасах, эзэмшил шилжүүлэх (`/transfer`), устгах.
- Төсөл (project) — workspace доторх хурлын ангилал.
- Team багц workspace-д: тэр workspace-ийн хурал workspace-ийн минутаас хасагдана.

## 04 Админ (`admins.py`, migration 0010)

| Түвшин | Хэн | Эрх |
|---|---|---|
| Үндсэн админ | `ADMIN_EMAILS` env | Бүгд + бусад админыг нэмэх/хасах |
| Нэмэгдсэн админ | `admin_grants` хүснэгт | Enterprise хүсэлт, санал хүсэлт, sandbox checkout |

Endpoint: `/api/admin/me`, `/admins`, `/contact-requests`.

## 05 Хүсэлтийн түвшний хамгаалалт
- `TrustedHostMiddleware` (`ALLOWED_HOSTS`), CORS (`CORS_ORIGINS`), WS Origin шалгалт.
- Security header-ууд, IP-ээр rate limit (production-д proxy-ийн `X-Forwarded-For`-д итгэнэ).
- Production-д `/api/docs`, `/api/openapi.json` унтраалттай.
- Алдааны дэлгэрэнгүй зөвхөн серверийн логт; клиентэд `CODE: мессеж` л очно.
