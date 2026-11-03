from __future__ import annotations

import pytest

from app.models.schemas import (
    AudioChunkMeta,
    MeetingCreate,
    MeetingResponse,
    MeetingStatus,
    WSAck,
    WSAudioMessage,
    WSError,
    WSProcessingStatus,
    WSRecordingReady,
)


def test_meeting_create_schema():
    data = MeetingCreate(title="Test Meeting")
    assert data.title == "Test Meeting"
    assert data.language == "mn"


def test_meeting_response_schema():
    from datetime import datetime, timezone
    data = MeetingResponse(
        id="test-id",
        title="Test",
        status=MeetingStatus.DRAFT,
        language="mn",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert data.status == MeetingStatus.DRAFT
    assert data.duration_ms == 0


def test_ws_ack_schema():
    ack = WSAck(sequence=5, meeting_id="test-id")
    assert ack.type == "audio.ack"
    assert ack.sequence == 5


def test_ws_audio_message_schema():
    msg = WSAudioMessage(
        meeting_id="test-id",
        sequence=1,
        mime_type="audio/webm",
        timestamp_ms=3000,
    )
    assert msg.type == "audio.chunk"


def test_ws_error_schema():
    err = WSError(code="ASR_UNAVAILABLE", message="Transcription temporarily unavailable")
    assert err.type == "error"
    assert err.code == "ASR_UNAVAILABLE"


def test_ws_recording_ready():
    msg = WSRecordingReady(meeting_id="test-id")
    assert msg.type == "recording.ready"


def test_ws_processing_status():
    msg = WSProcessingStatus(stage="transcribing", queued_chunks=3)
    assert msg.stage == "transcribing"
    assert msg.queued_chunks == 3
