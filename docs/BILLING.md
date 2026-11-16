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

## Wire тохируулах (нэг удаа) — docs.wire.mn
Wire-д **test горим байхгүй**: API зөвхөн `sk_live_…` түлхүүр хүлээн авна (`sk_test_` → `401 test_mode_unavailable`).
Тиймээс эхний шалгалт бол жинхэнэ, хамгийн бага (Starter) төлбөр.

1. `app.wire.mn` → Project → Keys: `sk_live_…` түлхүүр үүсгэнэ. Түлхүүрийг chat, git, мессежэд **хэзээ ч** бичихгүй.
2. Render → minuto service → Environment: `WIRE_API_KEY`, `FRONTEND_URL=https://mongolian-ai-minutes.vercel.app` → Save.
3. Webhook бүртгэх (Render → minuto → **Shell**):
   `python -m app.wire_setup`
   Гарч ирэх `WIRE_WEBHOOK_SECRET=whsec_…` мөрийг Render env-д нэмнэ (дахин харагдахгүй) → Save (сервис дахин эхэлнэ).
4. Wire dashboard дээр шинэ endpoint-ийг **Verify** дарна (Wire гарын үсэгтэй ping явуулна, бид 200 буцаана).
5. `BILLING_CHECKOUT_ENABLED=true` → Starter-ээр нэг удаа өөрөө төлж шалгана.

Төлбөрийн урсгал: PaymentIntent → Hosted checkout (`pay.wire.mn/c/…`). Худалдан авагч тэнд QPay болон бусад операторыг сонгоно.
Intent 10 минутад дуусна (захиалга `cancelled` болно; дахин "Төлөх" дарвал шинэ захиалга).

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
| 4 | Wire `sk_live_` түлхүүр (Wire-д test горим байхгүй) + `BILLING_CHECKOUT_ENABLED=true` → Starter-ээр өөрөө нэг удаа төлж шалгах | товч нээгдэнэ |
| 5 | `BILLING_METERING=enforce` (shadow-ийн `usage_events`-ийг 1 долоо хоног харсны дараа) | жинхэнэ шилжилт |

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

## Одоогийн төлөв (2026-11-16)
Checkout нээлттэй (live Wire), нэг Starter төлбөр амжилттай. `BILLING_METERING=shadow` асаалттай — минут тоологдож байна,
юуг ч хаахгүй. Дараагийн алхам: 1 долоо хоногийн дараа `usage_events`-ийг (`decision='would_block'`) харж `enforce`.
