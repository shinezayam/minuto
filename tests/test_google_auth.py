"""Google-ээр нэвтрэх: ID token шалгалт (гарын үсэг, aud, iss, exp, email_verified), бүртгэл/холболт. Сүлжээгүй:
Google-ийн JWKS-ийн оронд локал RSA түлхүүр."""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings
from app.services import auth

CLIENT_ID = "1234-test.apps.googleusercontent.com"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeJWKS:
    def get_signing_key_from_jwt(self, token):
        class K:
            key = _KEY.public_key()
        return K()


@pytest.fixture(autouse=True)
def google(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(auth, "_jwk_client", _FakeJWKS())


def credential(sub="g-123", email="bat@gmail.com", verified=True, aud=CLIENT_ID, iss="https://accounts.google.com",
               exp_in=3600, key=_KEY, name="Бат Болд") -> str:
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": sub, "email": email, "email_verified": verified, "name": name,
              "iat": now, "exp": now + exp_in}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


async def test_config_endpoint(anon_client, monkeypatch):
    assert (await anon_client.get("/api/auth/google/config")).json() == {"enabled": True, "client_id": CLIENT_ID}
    monkeypatch.setattr(settings, "google_client_id", "")
    assert (await anon_client.get("/api/auth/google/config")).json() == {"enabled": False, "client_id": None}
    r = await anon_client.post("/api/auth/google", json={"credential": credential()})
    assert r.status_code == 404 and r.json()["detail"].startswith("GOOGLE_DISABLED")


async def test_new_user_is_registered_then_logs_in(anon_client):
    r = await anon_client.post("/api/auth/google", json={"credential": credential()})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["email"] == "bat@gmail.com" and body["user"]["name"] == "Бат Болд" and body["token"]
    me = await anon_client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200

    again = await anon_client.post("/api/auth/google", json={"credential": credential()})
    assert again.status_code == 200 and again.json()["user"]["id"] == body["user"]["id"]
    # Google бүртгэлд нууц үг байхгүй: нууц үгээр нэвтрэх боломжгүй
    pw = await anon_client.post("/api/auth/login", json={"email": "bat@gmail.com", "password": "google$0$-$-"})
    assert pw.status_code == 401


async def test_existing_password_account_is_linked(anon_client):
    reg = await anon_client.post("/api/auth/register", json={"email": "Old@Gmail.com", "password": "password123"})
    uid = reg.json()["user"]["id"]
    r = await anon_client.post("/api/auth/google", json={"credential": credential(email="old@gmail.com", sub="g-old")})
    assert r.status_code == 200 and r.json()["user"]["id"] == uid
    # нууц үгээр ч нэвтэрсээр байна
    assert (await anon_client.post("/api/auth/login", json={"email": "old@gmail.com", "password": "password123"})).status_code == 200
    # өөр Google бүртгэл ижил и-мэйлээр холбогдохгүй
    r = await anon_client.post("/api/auth/google", json={"credential": credential(email="old@gmail.com", sub="g-other")})
    assert r.status_code == 409


@pytest.mark.parametrize("kwargs,code", [
    ({"aud": "someone-else.apps.googleusercontent.com"}, "GOOGLE_INVALID"),
    ({"iss": "https://evil.example"}, "GOOGLE_INVALID"),
    ({"exp_in": -120}, "GOOGLE_INVALID"),
    ({"key": _OTHER_KEY}, "GOOGLE_INVALID"),
    ({"verified": False}, "GOOGLE_EMAIL_UNVERIFIED"),
])
async def test_bad_credentials_are_rejected(anon_client, kwargs, code):
    r = await anon_client.post("/api/auth/google", json={"credential": credential(**kwargs)})
    assert r.status_code == 401 and r.json()["detail"].startswith(code)


async def test_registration_closed_blocks_new_google_users_only(anon_client, monkeypatch):
    await anon_client.post("/api/auth/register", json={"email": "member@gmail.com", "password": "password123"})
    monkeypatch.setattr(settings, "allow_registration", False)
    r = await anon_client.post("/api/auth/google", json={"credential": credential(email="new@gmail.com", sub="g-new")})
    assert r.status_code == 403
    r = await anon_client.post("/api/auth/google", json={"credential": credential(email="member@gmail.com", sub="g-m")})
    assert r.status_code == 200
