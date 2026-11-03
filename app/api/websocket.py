from __future__ import annotations

import json
import logging
from typing import Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.models.schemas import WSAck, WSError, WSRecordingReady
from app.services.audio_ingest import audio_ingest
from app.services import meeting_store

logger = logging.getLogger(__name__)
router = APIRouter()

# Track active WebSocket connections per meeting
_active_connections: Dict[str, Set[WebSocket]] = {}


@router.websocket("/ws/meetings/{meeting_id}/audio")
async def audio_websocket(websocket: WebSocket, meeting_id: str):
    await websocket.accept()

    meeting = meeting_store.get_meeting(meeting_id)
    if not meeting:
        await websocket.send_json(
            WSError(code="MEETING_NOT_FOUND", message="Meeting not found").model_dump()
        )
        await websocket.close(code=4404)
        return

    if meeting_id not in _active_connections:
        _active_connections[meeting_id] = set()
    _active_connections[meeting_id].add(websocket)

    try:
        await websocket.send_json(
            WSRecordingReady(meeting_id=meeting_id).model_dump()
        )

        while True:
            try:
                message = await websocket.receive()
            except RuntimeError as e:
                if "disconnect" in str(e).lower():
                    break
                raise

            if "text" in message and message["text"]:
                meta = json.loads(message["text"])
                msg_type = meta.get("type")

                if msg_type == "audio.chunk":
                    binary_data = await websocket.receive_bytes()
                    try:
                        result = await audio_ingest.accept_chunk(
                            meeting_id=meeting_id,
                            sequence=meta["sequence"],
                            mime_type=meta.get("mime_type", "audio/webm"),
                            timestamp_ms=meta.get("timestamp_ms", 0),
                            data=binary_data,
                        )
                        ack = WSAck(
                            sequence=meta["sequence"],
                            meeting_id=meeting_id,
                        )
                        await websocket.send_json(ack.model_dump())
                    except ValueError as e:
                        err = WSError(code="CHUNK_REJECTED", message=str(e))
                        await websocket.send_json(err.model_dump())

            elif "bytes" in message and message["bytes"]:
                binary_data = message["bytes"]
                if len(binary_data) > settings.max_audio_chunk_bytes:
                    err = WSError(
                        code="CHUNK_TOO_LARGE",
                        message=f"Max size: {settings.max_audio_chunk_bytes} bytes",
                    )
                    await websocket.send_json(err.model_dump())
                    continue

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for meeting {meeting_id}")
    except Exception as e:
        logger.error(f"WebSocket error for meeting {meeting_id}: {e}")
        try:
            err = WSError(code="INTERNAL_ERROR", message="Server error")
            await websocket.send_json(err.model_dump())
        except Exception:
            pass
    finally:
        if meeting_id in _active_connections:
            _active_connections[meeting_id].discard(websocket)
            if not _active_connections[meeting_id]:
                del _active_connections[meeting_id]
