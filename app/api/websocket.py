from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.models.schemas import WSAck, WSError, WSRecordingReady
from app.services.audio_ingest import audio_ingest
from app.services import live_transcriber, meeting_store, processing
from app.services.auth import user_from_token

logger = logging.getLogger(__name__)
router = APIRouter()

# Track active WebSocket connections per meeting
_active_connections: Dict[str, Set[WebSocket]] = {}


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_json(WSError(code=code, message=message).model_dump())


@router.websocket("/ws/meetings/{meeting_id}/audio")
async def audio_websocket(websocket: WebSocket, meeting_id: str):
    """
    Протокол: хэсэг бүрд эхлээд JSON meta
      {"type": "audio.chunk", "sequence": N, "mime_type": "...", "timestamp_ms": T}
    дараа нь шууд binary аудио. Хадгалсны дараа {"type": "audio.ack", "sequence": N} буцаана.
    Ижил sequence дахин ирвэл (дахин холбогдсоны дараа) дарж бичээд дахин ack өгнө.
    """
    await websocket.accept()

    # Хөтчийн WebSocket header явуулж чаддаггүй тул токенийг ?token=... хэлбэрээр авна
    user = user_from_token(websocket.query_params.get("token"))
    if not user:
        await _send_error(websocket, "UNAUTHORIZED", "Нэвтэрнэ үү")
        await websocket.close(code=4401)
        return

    meeting = meeting_store.get_meeting(meeting_id, user.id)
    if not meeting:
        await _send_error(websocket, "MEETING_NOT_FOUND", "Meeting not found")
        await websocket.close(code=4404)
        return

    _active_connections.setdefault(meeting_id, set()).add(websocket)
    pending_meta: Optional[dict] = None

    # Шууд бичвэр ар талын task-аас ирэх тул бүх илгээлтийг нэг lock-оор дараалуулна
    send_lock = asyncio.Lock()

    async def send(msg: dict) -> None:
        async with send_lock:
            await websocket.send_json(msg)

    live = None
    if meeting.status in ("recording", "paused"):
        live = live_transcriber.get_or_create(meeting_id, processing.meeting_dir(meeting_id))

    try:
        await send(WSRecordingReady(meeting_id=meeting_id).model_dump())
        if live is not None:
            # Дахин холбогдсон бол өмнө гарсан бичвэрийг бүгдийг нь дахин илгээнэ (клиент id-аар давхардлыг арилгана)
            for seg in list(live.segments):
                await send(seg)
            live.listeners.add(send)

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            text = message.get("text")
            data = message.get("bytes")

            if text is not None:
                try:
                    meta = json.loads(text)
                except json.JSONDecodeError:
                    await send(WSError(code="BAD_MESSAGE", message="Invalid JSON").model_dump())
                    continue

                if meta.get("type") != "audio.chunk":
                    continue  # одоогоор өөр төрлийн мессеж хүлээж авахгүй
                if not isinstance(meta.get("sequence"), int) or meta["sequence"] < 0:
                    await send(WSError(code="BAD_MESSAGE", message="sequence must be a non-negative integer").model_dump())
                    continue
                if pending_meta is not None:
                    # Өмнөх meta-гийн аудио ирээгүй — тэр хэсгийг алгасаж, клиент ack авахгүй тул дахин илгээнэ
                    logger.warning("meeting %s: chunk %s had no audio payload", meeting_id, pending_meta["sequence"])
                pending_meta = meta
                continue

            if data is not None:
                if pending_meta is None:
                    await send(WSError(code="CHUNK_WITHOUT_META", message="Audio received without preceding metadata").model_dump())
                    continue
                meta, pending_meta = pending_meta, None

                if len(data) > settings.max_audio_chunk_bytes:
                    await send(WSError(code="CHUNK_TOO_LARGE", message=f"Max size: {settings.max_audio_chunk_bytes} bytes").model_dump())
                    continue
                try:
                    await audio_ingest.accept_chunk(
                        meeting_id=meeting_id,
                        sequence=meta["sequence"],
                        mime_type=meta.get("mime_type") or "audio/webm",
                        timestamp_ms=int(meta.get("timestamp_ms") or 0),
                        data=data,
                    )
                except ValueError as e:
                    await send(WSError(code="CHUNK_REJECTED", message=str(e)).model_dump())
                    continue

                await send(WSAck(sequence=meta["sequence"], meeting_id=meeting_id).model_dump())
                if live is not None:
                    await live.notify_new_audio()

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error for meeting %s", meeting_id)
        try:
            await send(WSError(code="INTERNAL_ERROR", message="Server error").model_dump())
        except Exception:
            pass
    finally:
        if live is not None:
            live.listeners.discard(send)
        logger.info("WebSocket closed for meeting %s", meeting_id)
        conns = _active_connections.get(meeting_id)
        if conns is not None:
            conns.discard(websocket)
            if not conns:
                del _active_connections[meeting_id]
