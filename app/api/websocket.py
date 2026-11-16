from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, Optional, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.security import ws_origin_allowed
from app.services import access
from app.models.schemas import WSAck, WSError, WSRecordingReady
from app.services.audio_ingest import audio_ingest
from app.services import live_transcriber, meeting_store, processing
from app.services import upload_guard as guard
from app.services.auth import user_from_token, verify_ticket

logger = logging.getLogger(__name__)
router = APIRouter()

# Track active WebSocket connections per meeting
_active_connections: Dict[str, Set[WebSocket]] = {}

# Нэг дор ирэх sequence-ийн дээд үсрэлт (дахин холболтын дараа resend хийхэд хангалттай)
MAX_SEQUENCE_JUMP = 1000
MAX_SEQUENCE = 200_000


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
    # Өөр сайтаас (CSWSH) ирсэн холболтыг accept хийхээс өмнө татгалзана
    if not ws_origin_allowed(websocket.headers.get("origin")):
        logger.warning("WS rejected: origin %s", websocket.headers.get("origin"))
        await websocket.close(code=4403)
        return

    await websocket.accept()

    # Хөтчийн WebSocket header явуулж чаддаггүй тул богино настай нэг удаагийн ?ticket=... авна.
    # (Хуучин ?token= нь шилжилтийн хугацаанд л хүчинтэй — settings.allow_query_token)
    user = verify_ticket(websocket.query_params.get("ticket"), "ws", meeting_id, single_use=True)
    if user is None and settings.allow_query_token and websocket.query_params.get("token"):
        logger.warning("Deprecated WS ?token= used for meeting %s", meeting_id)
        user = user_from_token(websocket.query_params.get("token"))
    if not user:
        await _send_error(websocket, "UNAUTHORIZED", "Нэвтэрнэ үү")
        await websocket.close(code=4401)
        return

    try:
        meeting = meeting_store.get_meeting(meeting_id, user.id, "edit")  # шууд бичлэг = засах эрх (viewer бичлэг оруулж болохгүй)
    except access.AccessDenied:
        await _send_error(websocket, "FORBIDDEN", "Танд энэ хурал дээр бичлэг хийх эрх байхгүй")
        await websocket.close(code=4403)
        return
    if not meeting:
        await _send_error(websocket, "MEETING_NOT_FOUND", "Meeting not found")
        await websocket.close(code=4404)
        return

    # Диск дүүрсэн / квот хэтэрсэн бол шинэ бичлэгийг эхлүүлэхгүй
    try:
        guard.free_disk_ok()
        guard.check_quota(meeting_store.meeting_ids(user.id))
    except guard.GuardError as e:
        await _send_error(websocket, e.code, e.message)
        await websocket.close(code=4413)
        return

    _active_connections.setdefault(meeting_id, set()).add(websocket)
    pending_meta: Optional[dict] = None
    # Дахин холбогдсон бол өмнө хадгалагдсан хэмжээнээс үргэлжлүүлж тооцно
    total_bytes = guard.dir_bytes(processing.meeting_dir(meeting_id))
    next_expected_seq = 0

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
                seq = meta.get("sequence")
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                    await send(WSError(code="BAD_MESSAGE", message="sequence must be a non-negative integer").model_dump())
                    continue
                # Хэт том үсрэлт нь хэсэг бүрт тусдаа файл үүсгэх (диск/inode дүүргэх) хэрэглээ
                if seq > next_expected_seq + MAX_SEQUENCE_JUMP or seq > MAX_SEQUENCE:
                    await send(WSError(code="BAD_SEQUENCE", message="Chunk sequence is out of range").model_dump())
                    continue
                mime = str(meta.get("mime_type") or "audio/webm").split(";")[0].strip().lower()
                if not mime.startswith("audio/"):
                    await send(WSError(code="UNSUPPORTED_MIME", message="Only audio chunks are accepted").model_dump())
                    continue
                try:
                    ts = int(meta.get("timestamp_ms") or 0)
                except (TypeError, ValueError):
                    ts = -1
                if ts < 0:
                    await send(WSError(code="BAD_MESSAGE", message="timestamp_ms must be a non-negative integer").model_dump())
                    continue
                if settings.max_recording_sec and ts > settings.max_recording_sec * 1000:
                    await send(WSError(code="RECORDING_TOO_LONG", message=f"Бичлэг хэт урт байна (дээд хугацаа {settings.max_recording_sec // 3600} цаг)").model_dump())
                    await websocket.close(code=4413)
                    break
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
                if settings.max_ws_total_bytes and total_bytes + len(data) > settings.max_ws_total_bytes:
                    await send(WSError(code="RECORDING_TOO_LARGE", message="Бичлэг хэт том болсон тул зогсоолоо. Одоо хүртэл бичигдсэн хэсэг хадгалагдсан.").model_dump())
                    await websocket.close(code=4413)
                    break
                try:
                    guard.free_disk_ok()
                except guard.GuardError as e:
                    await send(WSError(code=e.code, message=e.message).model_dump())
                    await websocket.close(code=4413)
                    break
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

                total_bytes += len(data)
                next_expected_seq = max(next_expected_seq, meta["sequence"] + 1)
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
