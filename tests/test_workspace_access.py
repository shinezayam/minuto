"""3.1: workspace-ийн эрхийн матриц (үүрэг × үйлдэл), байгууллага хоорондын тусгаарлалт, гишүүн удирдлагын дүрэм."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services import meeting_store, processing
from tests.conftest import register
from tests.test_upload_protection import wav_bytes

ROLES = ("owner", "admin", "editor", "viewer")


class World:
    """Нэг байгууллага (ws) + нэг гадны хүн + owner/admin/editor/viewer хэрэглэгчид, тус бүрийн өөрийн client."""

    def __init__(self) -> None:
        self.users: dict = {}
        self.clients: dict[str, AsyncClient] = {}
        self.ws_id = ""
        self.project_id = ""

    def c(self, who: str) -> AsyncClient:
        return self.clients[who]


@pytest.fixture
async def world():
    w = World()
    for who in (*ROLES, "outsider"):
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        reg = await register(client, f"{who}@org.mn")
        client.headers["Authorization"] = f"Bearer {reg['token']}"
        client.auth_token = reg["token"]  # type: ignore[attr-defined]
        w.users[who], w.clients[who] = reg["user"], client
    owner = w.c("owner")
    w.ws_id = (await owner.post("/api/workspaces", json={"name": "Org"})).json()["id"]
    for who in ("admin", "editor", "viewer"):
        r = await owner.post(f"/api/workspaces/{w.ws_id}/invite", json={"email": f"{who}@org.mn", "role": who})
        assert r.status_code == 200, r.text
    w.project_id = (await owner.post(f"/api/workspaces/{w.ws_id}/projects", json={"name": "P"})).json()["id"]
    yield w
    for client in w.clients.values():
        await client.aclose()
    await processing.wait_all()


async def _ws_meeting(w: World, creator: str = "owner", **extra) -> str:
    r = await w.c(creator).post("/api/meetings", json={"title": "Т", "workspace_id": w.ws_id, **extra})
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    meeting_store.update_meeting_transcript(
        mid, [{"id": "seg_1", "start_ms": 0, "end_ms": 1000, "text": "Сайн уу", "speaker": None, "turns": []}]
    )
    meeting_store.update_meeting_minutes(mid, {"title": "Х", "summary": "С", "action_items": [{"task": "т"}]})
    return mid


# ---------- Матриц: харах ----------

@pytest.mark.parametrize("role", ROLES)
async def test_every_member_can_view_and_export(world: World, role):
    mid = await _ws_meeting(world)
    c = world.c(role)
    for path in ("", "/transcript", "/minutes", "/transcript/revisions", "/export.docx"):
        r = await c.get(f"/api/meetings/{mid}{path}")
        assert r.status_code == 200, (role, path, r.status_code)
    assert (await c.get(f"/api/meetings/{mid}")).json()["my_role"] == role
    assert (await c.post(f"/api/meetings/{mid}/audio-url")).status_code == 200


# ---------- Матриц: засах ----------

EDIT_CALLS = [
    ("PATCH", "", {"title": "Шинэ"}),
    ("PUT", "/transcript", {"segments": [{"id": "seg_1", "text": "засвар"}]}),
    ("POST", "/transcript/replace", {"find": "Сайн", "replace": "Мэнд"}),
    ("PUT", "/action-items", {"action_items": [{"task": "шинэ"}]}),
    ("POST", "/start", None),
    ("POST", "/ws-ticket", None),
]


@pytest.mark.parametrize("role,allowed", [("owner", True), ("admin", True), ("editor", True), ("viewer", False)])
@pytest.mark.parametrize("method,path,body", EDIT_CALLS)
async def test_edit_actions_matrix(world: World, role, allowed, method, path, body):
    mid = await _ws_meeting(world)
    r = await world.c(role).request(method, f"/api/meetings/{mid}{path}", json=body)
    if allowed:
        assert r.status_code in (200, 202), (role, method, path, r.status_code, r.text)
    else:
        assert r.status_code == 403 and r.json()["detail"].startswith("FORBIDDEN"), (role, method, path, r.status_code)


@pytest.mark.parametrize("role,allowed", [("owner", True), ("admin", True), ("editor", True), ("viewer", False)])
async def test_upload_and_process_matrix(world: World, role, allowed):
    mid = await _ws_meeting(world)
    up = await world.c(role).post(f"/api/meetings/{mid}/upload", files={"file": ("a.wav", wav_bytes(), "audio/wav")})
    assert (up.status_code == 200) == allowed, (role, up.status_code, up.text)
    if not allowed:
        assert up.status_code == 403
        assert (await world.c(role).post(f"/api/meetings/{mid}/process")).status_code == 403
        assert (await world.c(role).post(f"/api/meetings/{mid}/topics")).status_code == 403


@pytest.mark.parametrize("role,allowed", [("owner", True), ("admin", True), ("editor", True), ("viewer", False)])
async def test_create_meeting_in_workspace_matrix(world: World, role, allowed):
    r = await world.c(role).post("/api/meetings", json={"title": "Т", "workspace_id": world.ws_id})
    assert r.status_code == (201 if allowed else 403), (role, r.text)


# ---------- Матриц: устгах / архивлах ----------

async def test_delete_and_archive_matrix(world: World):
    # viewer хэзээ ч, editor зөвхөн өөрийнхийг, admin/owner бүгдийг
    by_owner = await _ws_meeting(world, "owner")
    by_editor = await _ws_meeting(world, "editor")
    assert (await world.c("viewer").delete(f"/api/meetings/{by_editor}")).status_code == 403
    assert (await world.c("viewer").post(f"/api/meetings/{by_editor}/archive")).status_code == 403
    assert (await world.c("editor").delete(f"/api/meetings/{by_owner}")).status_code == 403  # бусдынх
    assert (await world.c("editor").post(f"/api/meetings/{by_owner}/archive")).status_code == 403
    assert (await world.c("editor").post(f"/api/meetings/{by_editor}/archive")).status_code == 200  # өөрийнх
    assert (await world.c("editor").delete(f"/api/meetings/{by_editor}")).status_code == 204
    assert (await world.c("admin").post(f"/api/meetings/{by_owner}/archive")).status_code == 200
    assert (await world.c("admin").delete(f"/api/meetings/{by_owner}")).status_code == 204
    other = await _ws_meeting(world, "editor")
    assert (await world.c("owner").delete(f"/api/meetings/{other}")).status_code == 204


# ---------- Байгууллага хоорондын тусгаарлалт: бүх route 404 ----------

OUTSIDER_CALLS = [
    ("GET", "", None), ("PATCH", "", {"title": "x"}), ("DELETE", "", None),
    ("POST", "/archive", None), ("POST", "/start", None), ("POST", "/process", None), ("POST", "/topics", None),
    ("GET", "/transcript", None), ("PUT", "/transcript", {"segments": [{"id": "seg_1", "text": "x"}]}),
    ("POST", "/transcript/replace", {"find": "a", "replace": "b"}),
    ("GET", "/transcript/revisions", None), ("POST", "/transcript/revisions/x/restore", None),
    ("GET", "/minutes", None), ("GET", "/audio", None), ("POST", "/audio-url", None), ("POST", "/ws-ticket", None),
    ("GET", "/export.docx", None), ("GET", "/export.pdf", None),
    ("PUT", "/action-items", {"action_items": [{"task": "x"}]}), ("POST", "/regenerate-summary", None),
]


@pytest.mark.parametrize("method,path,body", OUTSIDER_CALLS)
async def test_outsider_gets_404_everywhere(world: World, method, path, body):
    mid = await _ws_meeting(world)
    r = await world.c("outsider").request(method, f"/api/meetings/{mid}{path}", json=body)
    assert r.status_code == 404, (method, path, r.status_code, r.text)


async def test_outsider_cannot_list_ask_or_ws(world: World):
    mid = await _ws_meeting(world)
    out = world.c("outsider")
    assert (await out.get("/api/meetings")).json()["meetings"] == []
    assert (await out.get(f"/api/meetings?workspace_id={world.ws_id}")).json()["meetings"] == []
    assert (await out.post("/api/ask", json={"question": "Сайн?", "meeting_id": mid})).status_code == 404
    r = await out.post("/api/ask", json={"question": "Сайн уу гэж хэн хэлсэн бэ?"})
    assert "Сайн уу" not in r.text or r.status_code != 200 or mid not in r.text
    with TestClient(app) as tc, tc.websocket_connect(f"/ws/meetings/{mid}/audio?token={out.auth_token}") as ws:  # type: ignore[attr-defined]
        assert ws.receive_json()["code"] == "MEETING_NOT_FOUND"


async def test_viewer_websocket_recording_is_forbidden_editor_allowed(world: World):
    mid = await _ws_meeting(world)
    with TestClient(app) as tc, tc.websocket_connect(f"/ws/meetings/{mid}/audio?token={world.c('viewer').auth_token}") as ws:  # type: ignore[attr-defined]
        assert ws.receive_json()["code"] == "FORBIDDEN"
    with TestClient(app) as tc, tc.websocket_connect(f"/ws/meetings/{mid}/audio?token={world.c('editor').auth_token}") as ws:  # type: ignore[attr-defined]
        assert ws.receive_json()["type"] == "recording.ready"


async def test_ask_is_scoped_to_accessible_meetings(world: World):
    mid = await _ws_meeting(world)
    contents_member = meeting_store.list_meeting_contents(world.users["viewer"]["id"])
    contents_out = meeting_store.list_meeting_contents(world.users["outsider"]["id"])
    assert mid in {c["id"] for c in contents_member}
    assert mid not in {c["id"] for c in contents_out}


async def test_list_includes_workspace_meetings_for_members_only(world: World):
    mid = await _ws_meeting(world)
    personal = (await world.c("viewer").post("/api/meetings", json={"title": "Хувийн"})).json()["id"]
    got = {m["id"] for m in (await world.c("viewer").get("/api/meetings")).json()["meetings"]}
    assert {mid, personal} <= got
    only_ws = {m["id"] for m in (await world.c("viewer").get(f"/api/meetings?workspace_id={world.ws_id}")).json()["meetings"]}
    assert only_ws == {mid}
    # бусад гишүүд viewer-ийн хувийн хурлыг харахгүй
    assert personal not in {m["id"] for m in (await world.c("admin").get("/api/meetings")).json()["meetings"]}
    assert (await world.c("owner").get(f"/api/meetings/{personal}")).status_code == 404


# ---------- Хувийн хурал: өмнөхтэй яг ижил ----------

async def test_personal_meetings_behave_as_before(world: World):
    a = world.c("editor")
    mid = (await a.post("/api/meetings", json={"title": "Хувийн"})).json()["id"]
    m = (await a.get(f"/api/meetings/{mid}")).json()
    assert m["workspace_id"] is None and m["my_role"] == "owner"
    assert (await a.patch(f"/api/meetings/{mid}", json={"title": "x"})).status_code == 200
    # байгууллагын owner хүртэл хувийн хурлыг харахгүй
    assert (await world.c("owner").get(f"/api/meetings/{mid}")).status_code == 404
    assert (await a.delete(f"/api/meetings/{mid}")).status_code == 204


# ---------- Project ----------

async def test_project_must_belong_to_workspace(world: World):
    owner = world.c("owner")
    other_ws = (await owner.post("/api/workspaces", json={"name": "Other"})).json()["id"]
    other_project = (await owner.post(f"/api/workspaces/{other_ws}/projects", json={"name": "OP"})).json()["id"]
    bad = await owner.post("/api/meetings", json={"title": "Т", "workspace_id": world.ws_id, "project_id": other_project})
    assert bad.status_code == 422 and bad.json()["detail"].startswith("INVALID_PROJECT")
    assert (await owner.post("/api/meetings", json={"title": "Т", "project_id": world.project_id})).status_code == 422  # workspace-гүй project
    ok = await owner.post("/api/meetings", json={"title": "Т", "workspace_id": world.ws_id, "project_id": world.project_id})
    assert ok.status_code == 201 and ok.json()["project_id"] == world.project_id


async def test_non_member_cannot_create_in_workspace(world: World):
    r = await world.c("outsider").post("/api/meetings", json={"title": "Т", "workspace_id": world.ws_id})
    assert r.status_code == 404


# ---------- Гишүүн удирдлага ----------

async def _invite(w: World, actor: str, email: str, role: str):
    return await w.c(actor).post(f"/api/workspaces/{w.ws_id}/invite", json={"email": email, "role": role})


async def test_invite_rules(world: World):
    assert (await _invite(world, "admin", "outsider@org.mn", "admin")).status_code == 403  # admin admin-ийг урихгүй
    assert (await _invite(world, "editor", "outsider@org.mn", "viewer")).status_code == 403
    assert (await _invite(world, "viewer", "outsider@org.mn", "viewer")).status_code == 403
    assert (await _invite(world, "outsider", "outsider@org.mn", "viewer")).status_code == 404  # гишүүн биш
    assert (await _invite(world, "admin", "nobody@org.mn", "viewer")).status_code == 400
    assert (await _invite(world, "admin", "editor@org.mn", "viewer")).status_code == 409  # аль хэдийн гишүүн
    assert (await _invite(world, "admin", "outsider@org.mn", "editor")).status_code == 200


async def test_admin_cannot_remove_admin_or_owner(world: World):
    owner_id, admin_id, editor_id = (world.users[x]["id"] for x in ("owner", "admin", "editor"))
    ws = world.ws_id
    assert (await world.c("admin").delete(f"/api/workspaces/{ws}/members/{owner_id}")).status_code == 403
    assert (await world.c("owner").delete(f"/api/workspaces/{ws}/members/{owner_id}")).status_code == 403  # owner-ийг хэн ч
    # admin өөр admin-ийг хасахгүй (хоёр дахь admin үүсгэнэ)
    await world.c("owner").post(f"/api/workspaces/{ws}/invite", json={"email": "outsider@org.mn", "role": "admin"})
    out_id = world.users["outsider"]["id"]
    assert (await world.c("admin").delete(f"/api/workspaces/{ws}/members/{out_id}")).status_code == 403
    assert (await world.c("admin").delete(f"/api/workspaces/{ws}/members/{editor_id}")).status_code == 204
    assert (await world.c("owner").delete(f"/api/workspaces/{ws}/members/{admin_id}")).status_code == 204


async def test_member_can_leave_but_viewer_cannot_remove_others(world: World):
    ws = world.ws_id
    editor_id, viewer_id = world.users["editor"]["id"], world.users["viewer"]["id"]
    assert (await world.c("viewer").delete(f"/api/workspaces/{ws}/members/{editor_id}")).status_code == 403
    assert (await world.c("viewer").delete(f"/api/workspaces/{ws}/members/{viewer_id}")).status_code == 204  # өөрөө гарна
    assert (await world.c("viewer").get(f"/api/workspaces/{ws}")).status_code == 404  # гарсны дараа харахгүй


async def test_change_role_rules(world: World):
    ws = world.ws_id
    editor_id, admin_id, viewer_id = (world.users[x]["id"] for x in ("editor", "admin", "viewer"))
    patch = lambda actor, uid, role: world.c(actor).patch(f"/api/workspaces/{ws}/members/{uid}", json={"role": role})  # noqa: E731
    assert (await patch("admin", editor_id, "viewer")).status_code == 200
    assert (await patch("admin", editor_id, "admin")).status_code == 403  # admin дэвшүүлж чадахгүй
    assert (await patch("admin", admin_id, "viewer")).status_code == 403  # admin admin-ийг өөрчлөхгүй
    assert (await patch("editor", viewer_id, "editor")).status_code == 403
    assert (await patch("owner", editor_id, "admin")).status_code == 200
    assert (await patch("owner", world.users["owner"]["id"], "viewer")).status_code == 403
    # үүрэг солигдсоноор эрх шууд өөрчлөгдөнө
    mid = await _ws_meeting(world)
    assert (await world.c("viewer").delete(f"/api/meetings/{mid}")).status_code == 403
    await patch("owner", viewer_id, "editor")
    assert (await world.c("viewer").put(f"/api/meetings/{mid}/transcript", json={"segments": [{"id": "seg_1", "text": "x"}]})).status_code == 200


async def test_delete_workspace_and_transfer_are_owner_only(world: World):
    ws = world.ws_id
    admin_id = world.users["admin"]["id"]
    mid = await _ws_meeting(world, "editor")
    for who in ("admin", "editor", "viewer"):
        assert (await world.c(who).delete(f"/api/workspaces/{ws}")).status_code == 403
        assert (await world.c(who).post(f"/api/workspaces/{ws}/transfer", json={"user_id": admin_id})).status_code == 403
    assert (await world.c("outsider").delete(f"/api/workspaces/{ws}")).status_code == 404
    assert (await world.c("owner").post(f"/api/workspaces/{ws}/transfer", json={"user_id": world.users["outsider"]["id"]})).status_code == 422
    assert (await world.c("owner").post(f"/api/workspaces/{ws}/transfer", json={"user_id": admin_id})).status_code == 204
    me = {w["id"]: w["role"] for w in (await world.c("owner").get("/api/workspaces")).json()}
    assert me[ws] == "admin"  # хуучин эзэн admin болсон
    assert (await world.c("owner").delete(f"/api/workspaces/{ws}")).status_code == 403
    assert (await world.c("admin").delete(f"/api/workspaces/{ws}")).status_code == 204
    # хурал устаагүй: үүсгэсэн хүндээ хувийн хурал болж үлдсэн
    m = (await world.c("editor").get(f"/api/meetings/{mid}")).json()
    assert m["workspace_id"] is None and m["my_role"] == "owner"


async def test_personal_workspace_cannot_be_deleted_or_invited(world: World):
    personal = next(w["id"] for w in (await world.c("owner").get("/api/workspaces")).json() if w["workspace_type"] == "personal")
    assert (await world.c("owner").delete(f"/api/workspaces/{personal}")).status_code == 422
    assert (await world.c("owner").post(f"/api/workspaces/{personal}/invite", json={"email": "admin@org.mn", "role": "viewer"})).status_code == 422


async def test_non_member_project_and_member_endpoints_are_404(world: World):
    out, ws = world.c("outsider"), world.ws_id
    assert (await out.get(f"/api/workspaces/{ws}/members")).status_code == 404
    assert (await out.get(f"/api/workspaces/{ws}/projects")).status_code == 404
    assert (await out.post(f"/api/workspaces/{ws}/projects", json={"name": "x"})).status_code == 404
    assert (await out.delete(f"/api/workspaces/{ws}/projects/{world.project_id}")).status_code == 404
    assert (await world.c("viewer").post(f"/api/workspaces/{ws}/projects", json={"name": "x"})).status_code == 403
    assert (await world.c("editor").delete(f"/api/workspaces/{ws}/projects/{world.project_id}")).status_code == 403


async def test_deleting_project_keeps_meetings(world: World):
    mid = await _ws_meeting(world, "owner", project_id=world.project_id)
    assert (await world.c("owner").delete(f"/api/workspaces/{world.ws_id}/projects/{world.project_id}")).status_code == 204
    m = (await world.c("owner").get(f"/api/meetings/{mid}")).json()
    assert m["project_id"] is None and m["workspace_id"] == world.ws_id
