"""Billing API: багц, үлдэгдэл, төлбөр (Wire → QPay), webhook, Enterprise хүсэлт."""
from __future__ import annotations

import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.models.schemas import UserResponse
from app.services import billing, payments
from app.services.auth import current_user, token_from_request, user_from_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["billing"])


class CheckoutIn(BaseModel):
    plan_code: str = Field(min_length=1, max_length=20)
    workspace_id: Optional[str] = Field(default=None, max_length=36)


class CancelIn(BaseModel):
    workspace_id: Optional[str] = Field(default=None, max_length=36)


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    company: Optional[str] = Field(default=None, max_length=200)
    phone: Optional[str] = Field(default=None, max_length=40)
    message: str = Field(min_length=1, max_length=5000)


def _http(e: billing.BillingError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail)


@router.get("/api/billing/plans")
def get_plans() -> dict:
    return {"plans": billing.list_plans()}


@router.get("/api/billing/me")
def get_billing_me(workspace_id: Optional[str] = Query(default=None, max_length=36),
                   user: UserResponse = Depends(current_user)) -> dict:
    try:
        return billing.summary(user.id, workspace_id)
    except billing.BillingError as e:
        raise _http(e) from e


@router.post("/api/billing/checkout", status_code=201)
async def checkout(body: CheckoutIn, user: UserResponse = Depends(current_user)) -> dict:
    try:
        return await asyncio.to_thread(billing.create_checkout, user.id, body.plan_code, body.workspace_id)
    except billing.BillingError as e:
        raise _http(e) from e


@router.get("/api/billing/orders")
def list_orders(user: UserResponse = Depends(current_user)) -> dict:
    return {"orders": billing.list_orders(user.id)}


@router.get("/api/billing/orders/{order_id}")
async def get_order(order_id: str, user: UserResponse = Depends(current_user)) -> dict:
    try:
        return await asyncio.to_thread(billing.get_order, user.id, order_id)
    except billing.BillingError as e:
        raise _http(e) from e


@router.post("/api/billing/cancel")
def cancel(body: CancelIn, user: UserResponse = Depends(current_user)) -> dict:
    try:
        return billing.cancel(user.id, body.workspace_id)
    except billing.BillingError as e:
        raise _http(e) from e


@router.post("/api/billing/webhooks/wire")
async def wire_webhook(request: Request) -> dict:
    """Wire-ийн webhook. Гарын үсэг буруу бол 400. Биеийн агуулгад итгэхгүй: intent-ийг сервер талаас дахин шалгана."""
    body = await request.body()
    if len(body) > 256_000:
        raise HTTPException(status_code=413, detail="PAYLOAD_TOO_LARGE")
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        status = await asyncio.to_thread(billing.handle_webhook, body, headers)
    except payments.InvalidSignature:
        logger.warning("wire webhook: invalid signature")
        raise HTTPException(status_code=400, detail="INVALID_SIGNATURE")
    except billing.BillingError as e:
        raise _http(e) from e
    return {"received": True, "status": status}


class FeedbackIn(BaseModel):
    category: Literal["idea", "bug", "other"] = "idea"
    message: str = Field(min_length=1, max_length=5000)


@router.post("/api/feedback", status_code=201)
def feedback(body: FeedbackIn, user: UserResponse = Depends(current_user)) -> dict:
    try:
        rid = billing.create_feedback(user.id, user.name or "", user.email, body.category, body.message)
    except billing.BillingError as e:
        raise _http(e) from e
    return {"id": rid}


@router.post("/api/contact-requests", status_code=201)
def contact_request(body: ContactIn, request: Request) -> dict:
    user = user_from_token(token_from_request(request))  # заавал биш: нэвтэрсэн бол холбоно
    try:
        rid = billing.create_contact_request(body.name, body.email, body.message, body.company, body.phone,
                                             user.id if user else None)
    except billing.BillingError as e:
        raise _http(e) from e
    return {"id": rid}
