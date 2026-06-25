"""Build a typed exchange client from a stored ApiKey row.

Decryption happens here so the rest of the app never touches plaintext.
"""
from __future__ import annotations

from app.core.encryption import decrypt_packed
from app.models.api_key import ApiKey
from app.services.exchange.base import ExchangeClient
from app.services.exchange.bitget import BitgetClient
from app.services.exchange.bybit import BybitClient
from app.services.exchange.deriv import DerivClient


def build_client(api_key_row: ApiKey) -> ExchangeClient:
    # Allow if the key was verified (exchange enforces actual permissions at order time)
    # OR if the permissions array explicitly includes "trade" (default after creation).
    can_trade = (
        "trade" in (api_key_row.permissions or [])
        or (api_key_row.verified and api_key_row.is_active)
    )
    if not can_trade:
        raise PermissionError(
            "api key is not verified — verify the key in Settings before starting agents"
        )
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
    if exchange in ("deriv", "deriv_live"):
        is_demo = exchange == "deriv" or is_testnet
        return DerivClient(
            api_token=plaintext_key,
            app_id=plaintext_secret,
            is_demo=is_demo,
        )
    raise ValueError(f"unsupported exchange: {exchange}")
