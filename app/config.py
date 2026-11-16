from pydantic import model_validator
from pydantic_settings import BaseSettings

DEFAULT_QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


class Settings(BaseSettings):
    database_url: str = ""
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_audio_bucket: str = "meeting-audio-private"

    # ASR: "omni" (Qwen-Omni API) | "whisper" (локал Whisper) | "demo" (тодорхой шошготой хуурамч бичвэр)
    asr_provider: str = "demo"
    asr_model_id: str = ""
    asr_device: str = "auto"
    # Qwen-Omni загвар, нэг удаа илгээх аудионы урт (сек), зэрэг илгээх хүсэлтийн тоо
    omni_model: str = "qwen3-omni-flash"
    omni_chunk_sec: int = 30
    asr_concurrency: int = 12  # 30с хэсгийг зэрэг илгээх тоо; 6 мин бичлэг: 4 → 306с, 12 → 38с
    # ASR_PROVIDER=whisper үед
    whisper_model: str = "medium"

    # LLM (хурлын тэмдэглэл): "qwen" | "demo"
    llm_provider: str = "demo"
    qwen_api_key: str = ""
    # Олон улсын endpoint. Хятадынх: https://dashscope.aliyuncs.com/compatible-mode/v1
    qwen_base_url: str = DEFAULT_QWEN_BASE_URL
    qwen_model: str = "qwen-plus"
    qwen_timeout_sec: int = 180
    # Qwen3 загваруудын "бодох" горим. Асаавал хариу 10-20 дахин удааширдаг тул default-аар унтраана.
    qwen_enable_thinking: bool = False

    # Хурлын үеэр бичвэрийг шууд харуулах (ASR-ийг ойролцоогоор 2 дахин их хэрэглэнэ).
    # Цонх: хамгийн багадаа live_min_sec, чимээгүй хэсэг хайж эхлэх live_window_sec, заавал таслах live_max_sec
    live_transcription: bool = True
    live_min_sec: int = 8
    live_window_sec: int = 15
    live_max_sec: int = 25

    # ffmpeg-ийн зам. Хоосон бол PATH, дараа нь imageio-ffmpeg-ээс хайна.
    ffmpeg_path: str = ""

    # Нэвтрэлт: JWT гарын үсгийн нууц. Хоосон бол сервер эхлэх бүрт шинээр үүснэ (бүгд дахин нэвтэрнэ).
    jwt_secret: str = ""
    jwt_ttl_days: int = 30
    # ?token=... query-ээр нэвтрэх хуучин арга (зөвхөн audio/export). Frontend шинэчлэгдсэний дараа false болгоно.
    allow_query_token: bool = True
    ws_ticket_ttl_sec: int = 60  # WebSocket-ийн нэг удаагийн тасалбар
    audio_url_ttl_sec: int = 900  # <audio>-д зориулсан гарын үсэгтэй линк (range хүсэлтүүд үргэлжилнэ)
    allow_registration: bool = True
    # "Google-ээр нэвтрэх": Google Cloud → OAuth client (Web) ID. Нууц биш (frontend-д ч бий). Хоосон бол унтраалттай.
    # Таслалаар хэд хэдэн client ID зөвшөөрч болно.
    google_client_id: str = ""
    # Админ хэрэглэгчдийн и-мэйл (таслалаар). Enterprise хүсэлт зэрэг дотоод хуудсыг харна.
    admin_emails: str = ""

    cors_origins: str = "http://localhost:3000"
    # "production" үед /api/docs унтарч, нууц (JWT_SECRET) шаардлагатай. Render дээр RENDER env автоматаар production гэж тооцогдоно.
    environment: str = "development"
    # Урд талд найдвартай proxy (Render, Vercel) байвал X-Forwarded-For-ийн сүүлийн хаягийг клиентийн IP гэж үзнэ.
    trust_proxy: bool = False
    # Зөвшөөрөгдөх Host header-ууд (таслалаар). "*" = хязгааргүй.
    allowed_hosts: str = "*"
    # WebSocket-ийн Origin allow-list. Хоосон бол CORS_ORIGINS-ийг ашиглана.
    ws_allowed_origins: str = ""
    # Rate limit (минутад): ерөнхий / нэвтрэх, бүртгүүлэх / хүнд (AI, боловсруулалт) endpoint
    rate_limit_per_min: int = 120
    rate_limit_auth_per_min: int = 10
    rate_limit_expensive_per_min: int = 20
    max_audio_chunk_bytes: int = 5_000_000
    # Боловсруулалтын ажлын дараалал (durable jobs)
    job_workers: int = 2  # зэрэг боловсруулах хурлын тоо
    job_max_attempts: int = 3  # provider-ийн түр алдаанд дахин оролдох дээд тоо (анхных нь орно)
    job_lease_sec: int = 120  # worker энэ хугацаанд heartbeat өгөхгүй бол ажил "унасан" гэж үзэж дахин дараалалд оруулна
    job_heartbeat_sec: int = 30
    job_timeout_sec: int = 3600  # нэг ажлын нийт дээд хугацаа
    job_poll_sec: float = 2.0
    job_backoff_sec: float = 5.0  # retry-ийн анхны хүлээлт; оролдлого бүрт 2 дахин нэмэгдэнэ (дээд нь 120 сек)
    # Аудио хамгаалалт (env-ээр өөрчилнө). 0 = хязгааргүй (зөвхөн тест/дотоод хэрэглээнд)
    max_upload_bytes: int = 300_000_000  # нэг файл upload
    max_recording_sec: int = 4 * 3600  # нэг бичлэгийн урт (upload болон шууд бичлэг)
    max_ws_total_bytes: int = 400_000_000  # нэг хурлын шууд бичлэгийн нийт хэмжээ
    user_daily_upload_bytes: int = 2_000_000_000  # нэг хэрэглэгч өдөрт
    user_total_storage_bytes: int = 10_000_000_000  # нэг хэрэглэгчийн нийт аудио
    min_free_disk_bytes: int = 500_000_000  # диск энэнээс бага чөлөөтэй бол шинэ аудио авахгүй
    audio_storage_path: str = "./audio_storage"
    # "local" = зөвхөн сервер дээрх диск (Render free дээр deploy бүрт арилна) | "supabase" = Supabase Storage-д байнга хадгална
    # (диск нь ажлын кэш болно). supabase_url, supabase_service_role_key, supabase_audio_bucket (private) шаардлагатай.
    audio_storage_backend: str = "local"

    # ---- Billing (BILLING_PLAN.md). Default-оор бүгд унтраалттай: одоогийн үйл ажиллагаа өөрчлөгдөхгүй.
    # off = хүснэгтэд хүрэхгүй | shadow = минутыг бүртгэнэ, юуг ч хаахгүй | enforce = минут дууссан бол шинэ боловсруулалт хаана
    billing_metering: str = "off"
    # Төлбөр авах товчийг нээнэ (Wire түлхүүр тохируулсны дараа л true болгоно)
    billing_checkout_enabled: bool = False
    # Wire (wire.mn) төлбөрийн API: түлхүүр, webhook secret-ийг Render env-д л оруулна, git/chat-д хэзээ ч бичихгүй
    wire_api_key: str = ""
    wire_webhook_secret: str = ""
    wire_base_url: str = "https://api.wire.mn"
    # Төлсний дараа хөтчийг буцаах frontend хаяг. Хоосон бол CORS_ORIGINS-ийн эхнийх.
    frontend_url: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def _empty_to_default(self) -> "Settings":
        # Render зэрэг dashboard-д утгыг хашилттай ("sk-...") эсвэл зайтай хуулж оруулбал токен буруу болно
        self.qwen_api_key = self.qwen_api_key.strip().strip("\"'").strip()
        self.qwen_base_url = self.qwen_base_url.strip().strip("\"'").strip()
        # .env-д "QWEN_MODEL=" гэх мэт хоосон үлдсэн бол default-ыг хэрэглэнэ
        if not self.qwen_base_url:
            self.qwen_base_url = DEFAULT_QWEN_BASE_URL
        if not self.qwen_model:
            self.qwen_model = "qwen-plus"
        if not self.omni_model:
            self.omni_model = "qwen3-omni-flash"
        self.wire_api_key = self.wire_api_key.strip().strip("\"'").strip()
        self.wire_webhook_secret = self.wire_webhook_secret.strip().strip("\"'").strip()
        self.billing_metering = (self.billing_metering or "off").strip().lower()
        if self.billing_metering not in ("off", "shadow", "enforce"):
            raise ValueError("BILLING_METERING нь off | shadow | enforce байх ёстой")
        return self

    @property
    def public_frontend_url(self) -> str:
        if self.frontend_url.strip():
            return self.frontend_url.strip().rstrip("/")
        first = next((o.strip() for o in self.cors_origins.split(",") if o.strip()), "http://localhost:3000")
        return first.rstrip("/")

    @property
    def is_production(self) -> bool:
        import os

        return self.environment.strip().lower() == "production" or bool(os.environ.get("RENDER"))

    @property
    def proxy_trusted(self) -> bool:
        return self.trust_proxy or self.is_production

    def ws_origins(self) -> list[str]:
        raw = self.ws_allowed_origins or self.cors_origins
        return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


settings = Settings()
