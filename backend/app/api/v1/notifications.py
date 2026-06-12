from __future__ import annotations

import uuid

from beanie.operators import Set
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notification import NotificationPublic

router = APIRouter()


@router.get("", response_model=list[NotificationPublic])
async def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
):
    query = Notification.find(Notification.user_id == user.id)
    if unread_only:
        query = query.find(Notification.read == False)  # noqa: E712
    return await query.sort(-Notification.created_at).limit(limit).to_list()


@router.post("/{notif_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(notif_id: uuid.UUID, user: User = Depends(get_current_user)):
    n = await Notification.find_one(Notification.id == notif_id, Notification.user_id == user.id)
    if not n:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    n.read = True
    await n.save()


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(user: User = Depends(get_current_user)):
    await Notification.find(
        Notification.user_id == user.id,
        Notification.read == False,  # noqa: E712
    ).update(Set({Notification.read: True}))
