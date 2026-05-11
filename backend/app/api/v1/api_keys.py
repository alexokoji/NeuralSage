from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.encryption import encrypt_packed
from app.database import get_db
from app.models.api_key import ApiKey
from app.models.user import User
from app.schemas.api_key import ApiKeyCreate, ApiKeyPublic, ApiKeyVerifyResult
from app.services.exchange import build_client
from app.services.exchange.base import ExchangeError, InsufficientPermissions

router = APIRouter()


@router.get("", response_model=list[ApiKeyPublic])
async def list_keys(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc()))
    return result.scalars().all()


@router.post("", response_model=ApiKeyPublic, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if "withdraw" in body.permissions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="withdrawal permission is not allowed")

    # Each ciphertext carries its own AES-GCM nonce (`nonce.ciphertext` packed).
    # The `encryption_iv` column is kept as a versioning marker for the
    # encryption format ("v2-packed" => both fields are self-contained blobs).
    aad = str(user.id).encode()
    row = ApiKey(
        user_id=user.id,
        exchange=body.exchange,
        label=body.label,
        encrypted_api_key=encrypt_packed(body.api_key, associated_data=aad),
        encrypted_api_secret=encrypt_packed(body.api_secret, associated_data=aad),
        encryption_iv="v2-packed",
        permissions=list(body.permissions),
        is_testnet=body.is_testnet or body.exchange == "bybit_testnet",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.post("/{key_id}/verify", response_model=ApiKeyVerifyResult)
async def verify_key(
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")

    try:
        client = build_client(row)
        try:
            perms = await client.verify_permissions()
        finally:
            await client.close()
    except InsufficientPermissions as exc:
        row.verified = False
        row.is_active = False
        await db.commit()
        return ApiKeyVerifyResult(verified=False, permissions=list(row.permissions), error=str(exc))
    except (ExchangeError, PermissionError) as exc:
        return ApiKeyVerifyResult(verified=False, permissions=list(row.permissions), error=str(exc))

    row.verified = True
    row.permissions = perms
    row.last_verified_at = datetime.now(timezone.utc)
    await db.commit()
    return ApiKeyVerifyResult(verified=True, permissions=perms)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    await db.delete(row)
    await db.commit()
