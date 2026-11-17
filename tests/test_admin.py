"""Админ: Enterprise хүсэлтүүдийг зөвхөн ADMIN_EMAILS-д байгаа хэрэглэгч харж, төлөв солино."""
from __future__ import annotations

from app.config import settings
from tests.conftest import register


async def _login(anon_client, email):
    auth = await register(anon_client, email)
    return {"Authorization": f"Bearer {auth['token']}"}


async def test_only_admins_see_and_update_contact_requests(anon_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", " Boss@Minuto.mn , other@x.mn")
    admin = await _login(anon_client, "boss@minuto.mn")
    user = await _login(anon_client, "user@minuto.mn")
    for i in range(2):
        r = await anon_client.post("/api/contact-requests", json={"name": f"Байгууллага {i}", "email": f"c{i}@co.mn", "message": "50 хэрэглэгч"})
        assert r.status_code == 201

    assert (await anon_client.get("/api/admin/me", headers=admin)).json() == {"is_admin": True, "is_owner": True}
    assert (await anon_client.get("/api/admin/me", headers=user)).json() == {"is_admin": False, "is_owner": False}
    assert (await anon_client.get("/api/admin/contact-requests", headers=user)).status_code == 403
    assert (await anon_client.get("/api/admin/contact-requests")).status_code == 401

    rows = (await anon_client.get("/api/admin/contact-requests", headers=admin)).json()["requests"]
    assert len(rows) == 2 and rows[0]["name"] == "Байгууллага 1" and rows[0]["status"] == "new"

    r = await anon_client.patch(f"/api/admin/contact-requests/{rows[0]['id']}", json={"status": "contacted"}, headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "contacted"
    assert (await anon_client.patch(f"/api/admin/contact-requests/{rows[0]['id']}", json={"status": "bogus"}, headers=admin)).status_code == 422
    assert (await anon_client.patch(f"/api/admin/contact-requests/{rows[0]['id']}", json={"status": "closed"}, headers=user)).status_code == 403
    new_only = (await anon_client.get("/api/admin/contact-requests", params={"status": "new"}, headers=admin)).json()["requests"]
    assert [x["name"] for x in new_only] == ["Байгууллага 0"]


async def test_no_admins_configured_means_nobody_is_admin(anon_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "")
    h = await _login(anon_client, "a@minuto.mn")
    assert (await anon_client.get("/api/admin/me", headers=h)).json() == {"is_admin": False, "is_owner": False}
    assert (await anon_client.get("/api/admin/contact-requests", headers=h)).status_code == 403


async def test_feedback_from_settings_reaches_admin(anon_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "boss@minuto.mn")
    admin = await _login(anon_client, "boss@minuto.mn")
    user = await _login(anon_client, "fan@minuto.mn")
    assert (await anon_client.post("/api/feedback", json={"category": "idea", "message": "Сайн байна"})).status_code == 401
    assert (await anon_client.post("/api/feedback", json={"category": "nope", "message": "x"}, headers=user)).status_code == 422
    r = await anon_client.post("/api/feedback", json={"category": "bug", "message": "  Бичлэг тасарсан  "}, headers=user)
    assert r.status_code == 201
    await anon_client.post("/api/contact-requests", json={"name": "Байгууллага", "email": "c@co.mn", "message": "50 хэрэглэгч"})

    # Default нь Enterprise л (хуучин frontend өөрчлөгдөхгүй)
    ent = (await anon_client.get("/api/admin/contact-requests", headers=admin)).json()["requests"]
    assert [x["kind"] for x in ent] == ["enterprise"]
    fb = (await anon_client.get("/api/admin/contact-requests", params={"kind": "feedback"}, headers=admin)).json()["requests"]
    assert len(fb) == 1 and fb[0]["email"] == "fan@minuto.mn" and fb[0]["category"] == "bug" and fb[0]["message"] == "Бичлэг тасарсан"
    both = (await anon_client.get("/api/admin/contact-requests", params={"kind": "all", "status": "new"}, headers=admin)).json()
    assert len(both["requests"]) == 2
    assert (await anon_client.get("/api/admin/contact-requests", params={"kind": "feedback"}, headers=user)).status_code == 403


async def test_feedback_daily_limit(anon_client, monkeypatch):
    from app.services import billing
    monkeypatch.setattr(billing, "FEEDBACK_DAILY_LIMIT", 2)
    user = await _login(anon_client, "chatty@minuto.mn")
    codes = [(await anon_client.post("/api/feedback", json={"message": f"санал {i}"}, headers=user)).status_code for i in range(3)]
    assert codes == [201, 201, 429]


async def test_only_owners_add_and_remove_admins(anon_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "owner1@minuto.mn, Owner2@minuto.mn")
    owner = await _login(anon_client, "owner1@minuto.mn")
    helper = await _login(anon_client, "helper@minuto.mn")
    user = await _login(anon_client, "user@minuto.mn")

    assert (await anon_client.get("/api/admin/me", headers=owner)).json() == {"is_admin": True, "is_owner": True}
    assert (await anon_client.get("/api/admin/me", headers=helper)).json() == {"is_admin": False, "is_owner": False}

    # Үндсэн админ шинэ админ нэмнэ
    r = await anon_client.post("/api/admin/admins", json={"email": " Helper@Minuto.mn "}, headers=owner)
    assert r.status_code == 201 and r.json()["email"] == "helper@minuto.mn"
    assert (await anon_client.post("/api/admin/admins", json={"email": "helper@minuto.mn"}, headers=owner)).status_code == 409
    assert (await anon_client.post("/api/admin/admins", json={"email": "owner2@minuto.mn"}, headers=owner)).status_code == 409
    assert (await anon_client.post("/api/admin/admins", json={"email": "not-an-email"}, headers=owner)).status_code == 422

    # Нэмэгдсэн админ хүсэлтүүдийг харна, гэхдээ админ нэмж/хасаж чадахгүй
    assert (await anon_client.get("/api/admin/me", headers=helper)).json() == {"is_admin": True, "is_owner": False}
    assert (await anon_client.get("/api/admin/contact-requests", headers=helper)).status_code == 200
    assert (await anon_client.get("/api/admin/admins", headers=helper)).status_code == 403
    assert (await anon_client.post("/api/admin/admins", json={"email": "user@minuto.mn"}, headers=helper)).status_code == 403
    assert (await anon_client.delete("/api/admin/admins/helper@minuto.mn", headers=helper)).status_code == 403
    assert (await anon_client.get("/api/admin/admins", headers=user)).status_code == 403

    body = (await anon_client.get("/api/admin/admins", headers=owner)).json()
    assert body["owners"] == ["owner1@minuto.mn", "owner2@minuto.mn"]
    assert [a["email"] for a in body["admins"]] == ["helper@minuto.mn"] and body["admins"][0]["added_by"] == "owner1@minuto.mn"

    # Үндсэн админыг сайтаас хасахгүй; нэмэгдсэнийг хасна
    assert (await anon_client.delete("/api/admin/admins/owner2@minuto.mn", headers=owner)).status_code == 409
    assert (await anon_client.delete("/api/admin/admins/helper@minuto.mn", headers=owner)).status_code == 204
    assert (await anon_client.get("/api/admin/me", headers=helper)).json()["is_admin"] is False
    assert (await anon_client.delete("/api/admin/admins/helper@minuto.mn", headers=owner)).status_code == 404
