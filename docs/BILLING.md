# Billing: минут тоолох ба Wire (QPay) төлбөр

Production backend: `https://minuto-ai-meeting-assistant.onrender.com`. Default тохиргоонд billing бүхэлдээ **унтраалттай**
(`BILLING_METERING=off`, `BILLING_CHECKOUT_ENABLED=false`), тэгэхээр deploy хийхэд хэрэглэгчийн үйл ажиллагаа өөрчлөгдөхгүй.
Зөвхөн migration `0005` ажиллаж, шинэ хүснэгтүүд үүснэ.

## Багц (migration 0005-д seed, үнийг клиентээс хэзээ ч авахгүй)
| code | минут | хугацаа | үнэ | хамрах хүрээ |
|---|---|---|---|---|
| free | 30 | нэг удаа | 0₮ | хэрэглэгч |
| starter | 120 | 30 хоног | 29,900₮ | хэрэглэгч |
| pro | 360 | 30 хоног | 69,900₮ | хэрэглэгч |
| team | 900 | 30 хоног | 149,900₮ | workspace |
| enterprise | гэрээгээр | – | – | үнийн санал (contact form) |

## Дүрэм (хамтрагчийн §H саналууд, батлагдсан)
- Нэгж: боловсруулсан аудионы урт, хурал бүрээр минут руу **дээш** бүхэлчилнэ (хамгийн багадаа 1). Уртыг файлаас ffmpeg-ээр уншина.
- Боловсруулалт эхлэхэд **reserve**. Бичвэр хадгалагдвал **debit** (хураангуй унасан ч), бичвэргүй дуусвал **release**.
- Ижил аудиог дахин боловсруулах үнэгүй (хурал + SHA-256). Аудио солигдвол шинээр тооцно.
- Минут дууссан үед шинэ upload боловсруулах болон шинэ шууд бичлэг эхлэхийг хаана (`402`).
  Аль хэдийн хийсэн **шууд бичлэгийг** хэзээ ч хаахгүй: хасах үлдэгдэл рүү орж болно. Upload нь үлдэгдлээс урт бол татгалзана.
- Team идэвхтэй workspace-ийн хурал workspace-ийн минутаас, бусад нь хурлын **эзний** хувийн минутаас хасагдана.
- Ашиглаагүй минут дараагийн хугацаанд шилжихгүй. Сунгалт одоогийн хугацаа дууссаны дараа эхэлнэ, ахиулалт шууд эхэлнэ.
- Цуцлах: хугацаа дуустал ашиглаад, дахин сунгахгүй. Буцаан олголт болон ахиулалтын үнийн зөрүүний дүрэм хараахан **батлагдаагүй** (кодод байхгүй).

## Wire тохируулах (нэг удаа)
1. Wire dashboard → API keys: **test** (`sk_test_…`) түлхүүр ав. Production-д дараа нь `sk_live_…`.
2. Wire dashboard → Webhooks → endpoint нэмнэ:
   `https://minuto-ai-meeting-assistant.onrender.com/api/billing/webhooks/wire`
   Events: `payment_intent.succeeded`, `payment_intent.payment_failed`, `payment_intent.canceled` (эсвэл бүгд). Үүсгэх үед
   гарч ирэх `whsec_…` secret-ийг хадгална (дахин харагдахгүй).
3. Render → minuto service → Environment (chat, git-д **хэзээ ч** бичихгүй):
   `WIRE_API_KEY`, `WIRE_WEBHOOK_SECRET`, `FRONTEND_URL=https://mongolian-ai-minutes.vercel.app`.

Webhook-ийн биеийн агуулгад итгэхгүй. Гарын үсгийг (`WirePayment-Signature`, HMAC-SHA256, 5 минутын tolerance) шалгасны дараа
intent-ийг сервер талаас `GET /v1/payment_intents/{id}`-ээр дахин уншина. Багц зөвхөн дараах гурван нөхцөл зэрэг биелэхэд идэвхжинэ:
`status=succeeded`, дүн захиалгатай таарсан, `metadata.order_id` таарсан. Давхар webhook ирсэн ч багцыг хоёр дахин өгөхгүй.
Webhook алдагдсан тохиолдолд сервер 10 минут тутам pending захиалгуудыг шалгана, frontend мөн 3 секунд тутам шалгана.

## Нэвтрүүлэх дараалал
| Алхам | Хийх | Нөлөө |
|---|---|---|
| 0 | DB backup (`pg_dump`). Render free Postgres автомат backup хийдэггүй | – |
| 1 | Merge → deploy. Migration `0005` эхлэхэд автоматаар ажиллана | зөвхөн шинэ хүснэгтүүд |
| 2 | `python -m app.migrate current` → `0005` эсэхийг шалгах | – |
| 3 | `BILLING_METERING=shadow`, 1 долоо хоног `usage_events`-ийг ажиглах (`decision=would_block` хэр олон гарч байгааг) | юуг ч хаахгүй |
| 4 | Wire **test** түлхүүр + `BILLING_CHECKOUT_ENABLED=true` → test төлбөрөөр бүрэн урсгалыг шалгах | товч нээгдэнэ |
| 5 | `sk_live_` түлхүүр, `BILLING_METERING=enforce` | жинхэнэ шилжилт |

Одоо байгаа хэрэглэгч бүр автоматаар нэг удаагийн 30 үнэгүй минуттай байна. Grant мөр хэрэггүй, учир нь үлдэгдлийг `30 - хэрэглэсэн` гэж тооцдог.

## Буцаах
- Хаалтыг шууд зогсоох: `BILLING_METERING=off` эсвэл `shadow`. Өгөгдөл өөрчлөгдөхгүй.
- ⚠ DB `0005` дээр байхад кодыг **шууд** буцааж болохгүй: хуучин код `0005`-г танихгүй тул эхэлж чадахгүй.
  Зөв дараалал: (1) шинэ код ажиллаж байхад Render Shell дээр `python -m app.migrate downgrade 0004` ажиллуулна
  (зөвхөн billing хүснэгтүүд устна, хэрэглэгчийн өгөгдөл хэвээр үлдэнэ); (2) дараа нь deploy-г буцаана.

## API
`GET /api/billing/plans` (нээлттэй) · `GET /api/billing/me[?workspace_id]` · `POST /api/billing/checkout {plan_code, workspace_id?}` ·
`GET /api/billing/orders[/{id}]` · `POST /api/billing/cancel` · `POST /api/billing/webhooks/wire` · `POST /api/contact-requests` (нээлттэй).
Enterprise хүсэлт `contact_requests` хүснэгтэд хадгалагдана. И-мэйл мэдэгдэл явуулахын тулд mail provider болон хүлээн авах хаяг хэрэгтэй (одоогоор хийгээгүй).
