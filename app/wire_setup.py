"""Wire webhook endpoint-ийг нэг удаа бүртгэнэ (Render Shell дээр ажиллуулна):

    python -m app.wire_setup https://minuto-ai-meeting-assistant.onrender.com/api/billing/webhooks/wire

WIRE_API_KEY env-ээс уншина. Хариуны whsec_… secret зөвхөн НЭГ удаа харагдана: хуулж аваад Render-ийн
WIRE_WEBHOOK_SECRET-д оруулж, сервис дахин эхэлсний дараа Wire dashboard дээрээс endpoint-оо verify хийнэ.
"""
from __future__ import annotations

import sys

from app.config import settings
from app.services.payments import ProviderRejected, ProviderUnavailable, WireProvider

DEFAULT_URL = "https://minuto-ai-meeting-assistant.onrender.com/api/billing/webhooks/wire"


def main(argv: list[str]) -> int:
    if not settings.wire_api_key:
        print("WIRE_API_KEY тохируулаагүй байна", file=sys.stderr)
        return 2
    url = argv[1] if len(argv) > 1 else DEFAULT_URL
    try:
        ep = WireProvider(settings.wire_api_key, "", settings.wire_base_url).register_webhook(url)
    except (ProviderRejected, ProviderUnavailable) as e:
        print(f"Бүртгэж чадсангүй: {e}", file=sys.stderr)
        return 1
    print(f"endpoint: {ep.get('id')}  status: {ep.get('status')}  url: {ep.get('url', url)}")
    print(f"WIRE_WEBHOOK_SECRET={ep.get('secret')}")
    print("↑ Энэ secret дахин харагдахгүй. Render env-д оруулаад, дараа нь Wire dashboard дээр endpoint-оо verify хийнэ үү.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
