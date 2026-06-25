from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.core.encryption import encrypt_packed
from app.models.api_key import ApiKey
from app.models.user import User
from app.schemas.api_key import ApiKeyCreate, ApiKeyPublic, ApiKeyVerifyResult
from app.services.exchange import build_client
from app.services.exchange.base import ExchangeError, InsufficientPermissions

router = APIRouter()


@router.get("", response_model=list[ApiKeyPublic])
async def list_keys(user: User = Depends(get_current_user)):
    return await ApiKey.find(ApiKey.user_id == user.id).sort(-ApiKey.created_at).to_list()


@router.post("", response_model=ApiKeyPublic, status_code=status.HTTP_201_CREATED)
async def create_key(body: ApiKeyCreate, user: User = Depends(get_current_user)):
    if "withdraw" in body.permissions:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="withdrawal permission is not allowed")

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
    await row.insert()
    return row


@router.post("/{key_id}/verify", response_model=ApiKeyVerifyResult)
async def verify_key(key_id: uuid.UUID, user: User = Depends(get_current_user)):
    row = await ApiKey.find_one(ApiKey.id == key_id, ApiKey.user_id == user.id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")

    # Build the client directly, bypassing the "trade permission required" guard
    # that build_client enforces — this endpoint exists specifically to check perms.
    from app.core.encryption import decrypt_packed
    from app.services.exchange.bybit import BybitClient
    from app.services.exchange.bitget import BitgetClient
    from app.services.exchange.deriv import DerivClient

    try:
        aad = str(row.user_id).encode()
        plaintext_key = decrypt_packed(row.encrypted_api_key, associated_data=aad)
        plaintext_secret = decrypt_packed(row.encrypted_api_secret, associated_data=aad)
        is_testnet = row.is_testnet or row.exchange == "bybit_testnet"
        if row.exchange in ("bybit", "bybit_testnet"):
            client = BybitClient(plaintext_key, plaintext_secret, is_testnet=is_testnet)
        elif row.exchange == "bitget":
            client = BitgetClient(plaintext_key, plaintext_secret, is_testnet=is_testnet)
        elif row.exchange in ("deriv", "deriv_live"):
            is_demo = row.exchange == "deriv" or is_testnet
            client = DerivClient(
                api_token=plaintext_key,
                app_id=plaintext_secret,
                is_demo=is_demo,
            )
        else:
            return ApiKeyVerifyResult(verified=False, permissions=[], error=f"unsupported exchange: {row.exchange}")
    except Exception as exc:
        return ApiKeyVerifyResult(verified=False, permissions=[], error=f"key decryption failed: {exc}")

    try:
        try:
            perms = await client.verify_permissions()
        finally:
            await client.close()
    except InsufficientPermissions as exc:
        row.verified = False
        row.is_active = False
        await row.save()
        return ApiKeyVerifyResult(verified=False, permissions=list(row.permissions), error=str(exc))
    except (ExchangeError, Exception) as exc:
        err = str(exc)
        # 403 means the server IP was blocked at WAF/CDN level before reaching
        # Bybit's auth layer. Common with cloud hosting + testnet.
        server_blocked = "403" in err
        return ApiKeyVerifyResult(
            verified=False,
            permissions=list(row.permissions),
            error=err,
            server_blocked=server_blocked,
        )

    row.verified = True
    row.is_active = True
    row.permissions = perms
    row.last_verified_at = datetime.now(timezone.utc)
    await row.save()
    return ApiKeyVerifyResult(verified=True, permissions=perms)


@router.post("/{key_id}/trust", response_model=ApiKeyPublic)
async def trust_key(key_id: uuid.UUID, user: User = Depends(get_current_user)):
    """Activate a key without exchange verification.

    Use when the exchange CDN blocks the server IP (common with Bybit testnet
    and cloud hosting). Invalid credentials will surface as errors in the agent
    activity log when the first order is attempted.
    """
    row = await ApiKey.find_one(ApiKey.id == key_id, ApiKey.user_id == user.id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    row.verified = True
    row.is_active = True
    row.last_verified_at = datetime.now(timezone.utc)
    await row.save()
    return row


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(key_id: uuid.UUID, user: User = Depends(get_current_user)):
    row = await ApiKey.find_one(ApiKey.id == key_id, ApiKey.user_id == user.id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    await row.delete()
