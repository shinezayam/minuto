from __future__ import annotations

import os
from typing import List

from app.models.minutes import MeetingMinutes, TranscriptSegment


class WhisperASRProvider:
    """Real ASR provider using OpenAI Whisper for Mongolian speech recognition.
    
    Note: For production use with Mongolian, consider using a Mongolian-fine-tuned
    Whisper model from Hugging Face (e.g., 'montagnext/whisper-mongolian' or similar).
    Set the model via WHISPER_MODEL environment variable.
    """

    def __init__(self, model_name: str = "base"):
        self.model_name = model_name
        self.model = None

    def _load_model(self):
        """Lazy load the Whisper model."""
        if self.model is None:
            import ssl
            import whisper
            # Disable SSL verification for model download (proxy/firewall workaround)
            ssl._create_default_https_context = ssl._create_unverified_context
            print(f"Loading Whisper model: {self.model_name}")
            self.model = whisper.load_model(self.model_name)
            print(f"✓ Whisper model loaded on {self.model.device}")

    async def transcribe(self, audio_path: str) -> List[TranscriptSegment]:
        """Transcribe audio file using Whisper."""
        try:
            self._load_model()

            # Let Whisper auto-detect language first
            # This works better than forcing language="mn" for small models
            result = self.model.transcribe(
                audio_path,
                task="transcribe",
                verbose=False,
                fp16=False  # Use fp32 for CPU
            )

            # Check detected language
            detected_language = result.get("language", "unknown")
            print(f"Detected language: {detected_language}")

            segments = []
            for i, segment in enumerate(result["segments"]):
                text = segment["text"].strip()
                if text:  # Only add non-empty segments
                    segments.append(
                        TranscriptSegment(
                            id=f"seg_{i+1}",
                            start_ms=int(segment["start"] * 1000),
                            end_ms=int(segment["end"] * 1000),
                            text=text,
                            speaker=None,
                        )
                    )

            return segments
        except Exception as e:
            print(f"Whisper transcription failed: {e}")
            print("Falling back to mock provider...")
            # Fall back to mock provider on error
            return await MockASRProvider().transcribe(audio_path)


class MockASRProvider:
    """Demo ASR provider that returns placeholder transcripts."""

    async def transcribe(self, audio_path: str) -> List[TranscriptSegment]:
        # In production, this would call Whisper or another ASR service
        # For now, return demo segments
        return [
            TranscriptSegment(
                id="seg_1",
                start_ms=0,
                end_ms=5000,
                text="Сайн байна уу. Энэ бол Монгол AI Minutes системийн демо бичлэг байна.",
                speaker=None,
            ),
            TranscriptSegment(
                id="seg_2",
                start_ms=5000,
                end_ms=12000,
                text="Бид энэ системийг хурлын бичиг автоматаар үүсгэх зорилгоор бүтээсэн.",
                speaker=None,
            ),
            TranscriptSegment(
                id="seg_3",
                start_ms=12000,
                end_ms=20000,
                text="Ирээдүйд бодит Mongolian ASR модел нэмэгдэх болно.",
                speaker=None,
            ),
        ]


class MockLLMProvider:
    """Demo LLM provider that generates meeting minutes from transcript."""

    async def generate_minutes(self, transcript: List[TranscriptSegment]) -> MeetingMinutes:
        # Combine all transcript text
        full_text = " ".join([seg.text for seg in transcript])

        # In production, this would call Qwen API
        # For now, generate demo minutes
        return MeetingMinutes(
            title="Демо хурлын бичиг",
            summary="Энэ бол демо хурлын бичиг юм. Бодит ASR болон LLM интеграци хийгдэх болно.",
            topics=[
                {
                    "title": "Системийн танилцуулга",
                    "description": "Mongolian AI Minutes системийн зорилго, боломжуудыг танилцуулав.",
                }
            ],
            decisions=[
                {
                    "text": "Бодит Mongolian ASR модел нэмэх",
                    "rationale": "Демо бичвэрийг бодит бичвэрээр солих",
                }
            ],
            action_items=[
                {
                    "task": "Whisper Mongolian модел интеграци хийх",
                    "owner": None,
                    "due_date": None,
                },
                {
                    "task": "Qwen LLM API нэмэх",
                    "owner": None,
                    "due_date": None,
                },
            ],
        )


class QwenLLMProvider:
    """Real LLM provider using Qwen via Alibaba Cloud Model Studio for meeting minutes generation."""

    def __init__(self):
        self.api_key = os.getenv("QWEN_API_KEY")
        self.base_url = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.model = os.getenv("QWEN_MODEL", "qwen-plus")

    async def generate_minutes(self, transcript: List[TranscriptSegment]) -> MeetingMinutes:
        """Generate meeting minutes from transcript using Qwen LLM."""
        if not self.api_key:
            print("Warning: QWEN_API_KEY not set, falling back to mock provider")
            return await MockLLMProvider().generate_minutes(transcript)

        import httpx

        # Combine transcript with timestamps
        transcript_text = "\n".join([
            f"[{seg.start_ms//1000}s - {seg.end_ms//1000}s] {seg.text}"
            for seg in transcript
        ])

        # System prompt for meeting minutes generation
        system_prompt = """You are a professional meeting secretary. Generate structured meeting minutes in Mongolian (Cyrillic script) from the transcript.

Return a JSON object with this structure:
{
  "title": "Meeting title in Mongolian",
  "summary": "Brief summary in Mongolian (2-3 sentences)",
  "topics": [{"title": "Topic title", "description": "Brief description"}],
  "decisions": [{"text": "Decision text", "rationale": "Why this decision was made"}],
  "action_items": [{"task": "Task description", "owner": "Person responsible or null", "due_date": "YYYY-MM-DD or null"}]
}

Important:
- All text must be in Mongolian Cyrillic script
- Extract actual topics, decisions, and action items from the transcript
- If information is not available, use empty arrays
- Be concise and professional"""

        user_prompt = f"Generate meeting minutes from this transcript:\n\n{transcript_text}"

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.3,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                result = response.json()

            # Parse the response
            content = result["choices"][0]["message"]["content"]
            import json
            minutes_data = json.loads(content)

            return MeetingMinutes(
                title=minutes_data.get("title", "Хурлын бичиг"),
                summary=minutes_data.get("summary", ""),
                topics=minutes_data.get("topics", []),
                decisions=minutes_data.get("decisions", []),
                action_items=minutes_data.get("action_items", []),
            )

        except Exception as e:
            print(f"Error calling Qwen API: {e}")
            # Fall back to mock on error
            return await MockLLMProvider().generate_minutes(transcript)


# Use real Whisper ASR if available, otherwise fall back to mock
ASR_PROVIDER = os.getenv("ASR_PROVIDER", "whisper")
if ASR_PROVIDER == "whisper":
    # Use medium model by default for better Mongolian support
    # For production, consider using a Mongolian-fine-tuned model
    asr_provider = WhisperASRProvider(model_name=os.getenv("WHISPER_MODEL", "medium"))
else:
    asr_provider = MockASRProvider()

# Use Qwen LLM if API key is available, otherwise fall back to mock
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "qwen")
if LLM_PROVIDER == "qwen" and os.getenv("QWEN_API_KEY"):
    llm_provider = QwenLLMProvider()
else:
    llm_provider = MockLLMProvider()
