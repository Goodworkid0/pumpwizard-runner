"""Explicit, fail-closed token layouts for manual native-SOL trading.

Token-2022 mints: immutable self-pointing MetadataPointer + TokenMetadata only.
Token accounts: base layout, optionally ImmutableOwner; all other TLVs rejected.
No metadata URI is fetched and metadata text is never used as execution input.
"""

from __future__ import annotations

import struct

from solders.pubkey import Pubkey

from .ownership import TOKEN_PROGRAM, TOKEN_2022_PROGRAM
from .transfers import TransferError


def extensions(raw: bytes, *, account_type: int, base_length: int) -> dict[int, bytes]:
    if len(raw) == base_length:
        return {}
    if (len(raw) < 166 or len(raw) > 16_384 or len(raw) == 355 or
            any(raw[base_length:165]) or raw[165] != account_type):
        raise TransferError("Unsupported Token-2022 account type, size, or padding")
    result = {}
    offset = 166
    while offset < len(raw):
        if not any(raw[offset:]):
            break
        if len(raw) - offset < 4:
            raise TransferError("Truncated Token-2022 extension header")
        kind, length = struct.unpack_from("<HH", raw, offset)
        offset += 4
        if kind == 0 or kind in result or offset + length > len(raw):
            raise TransferError("Invalid, duplicate, or truncated Token-2022 extension")
        result[kind] = raw[offset:offset + length]
        offset += length
    return result


def validate_mint(raw: bytes, mint: str, program: str) -> None:
    if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise TransferError("Unsupported mint token program")
    if (len(raw) < 82 or raw[44:46] != bytes([6, 1]) or
            raw[:4] != bytes(4) or raw[46:50] != bytes(4)):
        raise TransferError("Trading requires 6 decimals and revoked mint/freeze authorities")
    if program == TOKEN_PROGRAM:
        if len(raw) != 82:
            raise TransferError("Unsupported standard SPL mint layout")
        return
    tlv = extensions(raw, account_type=1, base_length=82)
    if set(tlv) != {18, 19}:
        raise TransferError("Token-2022 policy permits MetadataPointer and TokenMetadata only")
    identity = bytes(Pubkey.from_string(mint))
    if tlv[18] != bytes(32) + identity:
        raise TransferError("Token-2022 metadata pointer must be immutable and point to this mint")
    metadata = tlv[19]
    if len(metadata) < 80 or metadata[:64] != bytes(32) + identity:
        raise TransferError("Token-2022 metadata must be immutable and belong to this mint")
    offset = 64

    def number():
        nonlocal offset
        if offset + 4 > len(metadata):
            raise TransferError("Truncated Token-2022 metadata")
        value = int.from_bytes(metadata[offset:offset + 4], "little")
        offset += 4
        return value

    def string():
        nonlocal offset
        length = number()
        if offset + length > len(metadata):
            raise TransferError("Truncated Token-2022 metadata string")
        try:
            metadata[offset:offset + length].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise TransferError("Invalid Token-2022 metadata encoding") from None
        offset += length

    for _ in range(3):
        string()
    count = number()
    if count > (len(metadata) - offset) // 8:
        raise TransferError("Invalid Token-2022 metadata collection")
    for _ in range(count):
        string()
        string()
    if offset != len(metadata):
        raise TransferError("Trailing bytes in Token-2022 metadata")


def validate_token_extensions(raw: bytes, program: str) -> None:
    if program == TOKEN_PROGRAM:
        if len(raw) != 165:
            raise TransferError("Unsupported standard SPL token account layout")
    elif program == TOKEN_2022_PROGRAM:
        tlv = extensions(raw, account_type=2, base_length=165)
        if set(tlv) - {7} or (7 in tlv and tlv[7] != b""):
            raise TransferError("Token-2022 token accounts permit ImmutableOwner only")
    else:
        raise TransferError("Unsupported token account program")
