"""Build a typed exchange client from a stored ApiKey row.

Decryption happens here so the rest of the app never touches plaintext.
"""
from __future__ import annotations

from app.core.encryption import decrypt_packed
from app.models.api_key import ApiKey
from app.services.exchange.base import ExchangeClient
from app.services.exchange.bitget import BitgetClient
from app.services.exchange.bybit import BybitClient


def build_client(api_key_row: ApiKey) -> ExchangeClient:
    if "trade" not in (api_key_row.permissions or []):
        # Defense in depth: even if the DB row is corrupted, refuse trading.
        raise PermissionError("api key lacks 'trade' permission")
    if "withdraw" in (api_key_row.permissions or []):
        raise PermissionError("api key has withdraw permission; refusing to use")

    aad = str(api_key_row.user_id).encode()
    plaintext_key = decrypt_packed(api_key_row.encrypted_api_key, associated_data=aad)
    plaintext_secret = decrypt_packed(api_key_row.encrypted_api_secret, associated_data=aad)

    exchange = api_key_row.exchange
    is_testnet = api_key_row.is_testnet or exchange == "bybit_testnet"

    if exchange in ("bybit", "bybit_testnet"):
        return BybitClient(plaintext_key, plaintext_secret, is_testnet=is_testnet)
    if exchange == "bitget":
        return BitgetClient(plaintext_key, plaintext_secret, is_testnet=is_testnet)
    raise ValueError(f"unsupported exchange: {exchange}")
