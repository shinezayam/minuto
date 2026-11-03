from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    database_url: str = ""
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_audio_bucket: str = "meeting-audio-private"
    asr_provider: str = "demo"
    asr_model_id: str = ""
    asr_device: str = "auto"
    qwen_api_key: str = ""
    qwen_base_url: str = ""
    qwen_model: str = ""
    cors_origins: str = "http://localhost:3000"
    max_audio_chunk_bytes: int = 5_000_000
    audio_storage_path: str = "./audio_storage"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
