from __future__ import annotations

from typing import List

from app.models.minutes import MeetingMinutes, TranscriptSegment


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


asr_provider = MockASRProvider()
llm_provider = MockLLMProvider()
