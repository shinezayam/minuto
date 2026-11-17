"""App-ын бүх route-ыг хавтгай жагсаалтаар буцаана.

FastAPI 0.120+ include_router-ыг `_IncludedRouter` болгон хадгалдаг тул `app.routes` дотор APIRoute харагдахгүй.
Тэгэхэд route шалгадаг (нэвтрэлт, 204) тестүүд юу ч шалгалгүй давчихна — үүнээс сэргийлнэ.
"""
from __future__ import annotations


def iter_routes(router) -> list:
    out = []
    for r in router.routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            out.extend(iter_routes(inner))
        else:
            out.append(r)
    return out
