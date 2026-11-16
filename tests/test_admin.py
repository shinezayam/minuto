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

    assert (await anon_client.get("/api/admin/me", headers=admin)).json() == {"is_admin": True}
    assert (await anon_client.get("/api/admin/me", headers=user)).json() == {"is_admin": False}
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
    assert (await anon_client.get("/api/admin/me", headers=h)).json() == {"is_admin": False}
    assert (await anon_client.get("/api/admin/contact-requests", headers=h)).status_code == 403
