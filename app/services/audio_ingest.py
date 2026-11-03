from __future__ import annotations

import os
import struct
import time
from pathlib import Path
from typing import Dict, Optional

from app.config import settings


class AudioIngestService:
    """Receives, validates, and persists audio chunks per meeting."""

    def __init__(self) -> None:
        self._chunk_counts: Dict[str, int] = {}
        self._byte_counts: Dict[str, int] = {}
        self._storage_root = Path(settings.audio_storage_path)
        self._storage_root.mkdir(parents=True, exist_ok=True)

    def _meeting_dir(self, meeting_id: str) -> Path:
        d = self._storage_root / meeting_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def accept_chunk(
        self,
        meeting_id: str,
        sequence: int,
        mime_type: str,
        timestamp_ms: int,
        data: bytes,
    ) -> dict:
        if len(data) > settings.max_audio_chunk_bytes:
            raise ValueError(
                f"Chunk too large: {len(data)} > {settings.max_audio_chunk_bytes}"
            )

        expected_seq = self._chunk_counts.get(meeting_id, 0)
        if sequence < expected_seq:
            # Idempotent: chunk was already received (likely a reconnect resend
            # where the ack was lost). Overwrite the file and accept silently.
            pass

        meeting_dir = self._meeting_dir(meeting_id)
        ext = _mime_to_ext(mime_type)
        filename = f"{sequence:06d}{ext}"
        filepath = meeting_dir / filename

        loop = __import__("asyncio").get_event_loop()
        await loop.run_in_executor(None, filepath.write_bytes, data)

        # Only advance the counter for new chunks, not reconnect resends
        if sequence >= self._chunk_counts.get(meeting_id, 0):
            self._chunk_counts[meeting_id] = sequence + 1
        self._byte_counts[meeting_id] = self._byte_counts.get(meeting_id, 0) + len(data)

        return {
            "meeting_id": meeting_id,
            "sequence": sequence,
            "storage_path": str(filepath),
            "mime_type": mime_type,
            "size_bytes": len(data),
            "timestamp_ms": timestamp_ms,
        }

    def get_stats(self, meeting_id: str) -> dict:
        return {
            "meeting_id": meeting_id,
            "chunks_received": self._chunk_counts.get(meeting_id, 0),
            "total_bytes": self._byte_counts.get(meeting_id, 0),
        }

    def cleanup(self, meeting_id: str) -> None:
        self._chunk_counts.pop(meeting_id, None)
        self._byte_counts.pop(meeting_id, None)


def _mime_to_ext(mime_type: str) -> str:
    mapping = {
        "audio/webm": ".webm",
        "audio/webm;codecs=opus": ".webm",
        "audio/ogg": ".ogg",
        "audio/ogg;codecs=opus": ".ogg",
        "audio/wav": ".wav",
        "audio/wave": ".wav",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
    }
    return mapping.get(mime_type, ".bin")


audio_ingest = AudioIngestService()
