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
    asr_concurrency: int = 4
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

    # ffmpeg-ийн зам. Хоосон бол PATH, дараа нь imageio-ffmpeg-ээс хайна.
    ffmpeg_path: str = ""

    cors_origins: str = "http://localhost:3000"
    max_audio_chunk_bytes: int = 5_000_000
    audio_storage_path: str = "./audio_storage"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def _empty_to_default(self) -> "Settings":
        # .env-д "QWEN_MODEL=" гэх мэт хоосон үлдсэн бол default-ыг хэрэглэнэ
        if not self.qwen_base_url:
            self.qwen_base_url = DEFAULT_QWEN_BASE_URL
        if not self.qwen_model:
            self.qwen_model = "qwen-plus"
        if not self.omni_model:
            self.omni_model = "qwen3-omni-flash"
        return self


settings = Settings()
