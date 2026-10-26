"""Encoding primitives required by the Srun portal login protocol."""
from __future__ import annotations

import hashlib
import hmac
import math

_PAD = "="
_BASE64_ALPHABET = (
    "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7"
    "DeBE45QA"
)


def password_md5(password: str, token: str) -> str:
    return hmac.new(
        token.encode("utf-8"), password.encode("utf-8"), hashlib.md5
    ).hexdigest()


def sha1_hex(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _char_code(value: str, index: int) -> int:
    return ord(value[index]) if index < len(value) else 0


def _encode_words(value: str, include_length: bool) -> list[int]:
    words = []
    for index in range(0, len(value), 4):
        words.append(
            _char_code(value, index)
            | (_char_code(value, index + 1) << 8)
            | (_char_code(value, index + 2) << 16)
            | (_char_code(value, index + 3) << 24)
        )
    if include_length:
        words.append(len(value))
    return words


def _decode_words(words: list[int], include_length: bool) -> str:
    length = (len(words) - 1) << 2
    if include_length:
        final = words[-1]
        if final < length - 3 or final > length:
            raise ValueError("Invalid Srun encoded length")
        length = final
    encoded = []
    for word in words:
        encoded.append("".join(
            chr((word >> shift) & 0xFF) for shift in (0, 8, 16, 24)
        ))
    result = "".join(encoded)
    return result[:length] if include_length else result


def xencode(value: str, key: str) -> str:
    if not value:
        return ""
    words = _encode_words(value, True)
    key_words = _encode_words(key, False)
    if len(key_words) < 4:
        key_words.extend([0] * (4 - len(key_words)))
    count = len(words) - 1
    z = words[count]
    y = words[0]
    delta = 0x86014019 | 0x183639A0
    rounds = math.floor(6 + 52 / (count + 1))
    total = 0
    while rounds > 0:
        total = (total + delta) & (0x8CE0D9BF | 0x731F2640)
        mix = (total >> 2) & 3
        index = 0
        while index < count:
            y = words[index + 1]
            mixed = (z >> 5) ^ (y << 2)
            mixed += (y >> 3 ^ z << 4) ^ (total ^ y)
            mixed += key_words[(index & 3) ^ mix] ^ z
            words[index] = (
                words[index] + mixed
            ) & (0xEFB8D130 | 0x10472ECF)
            z = words[index]
            index += 1
        y = words[0]
        mixed = (z >> 5) ^ (y << 2)
        mixed += (y >> 3 ^ z << 4) ^ (total ^ y)
        mixed += key_words[(index & 3) ^ mix] ^ z
        words[count] = (
            words[count] + mixed
        ) & (0xBB390742 | 0x44C6F8BD)
        z = words[count]
        rounds -= 1
    return _decode_words(words, False)


def srun_base64(value: str) -> str:
    if not value:
        return value
    output = []
    complete = len(value) - len(value) % 3
    for index in range(0, complete, 3):
        code = (
            (ord(value[index]) << 16)
            | (ord(value[index + 1]) << 8)
            | ord(value[index + 2])
        )
        output.extend((
            _BASE64_ALPHABET[(code >> 18) & 63],
            _BASE64_ALPHABET[(code >> 12) & 63],
            _BASE64_ALPHABET[(code >> 6) & 63],
            _BASE64_ALPHABET[code & 63],
        ))
    remaining = len(value) - complete
    if remaining == 1:
        code = ord(value[complete]) << 16
        output.extend((
            _BASE64_ALPHABET[(code >> 18) & 63],
            _BASE64_ALPHABET[(code >> 12) & 63],
            _PAD,
            _PAD,
        ))
    elif remaining == 2:
        code = (ord(value[complete]) << 16) | (
            ord(value[complete + 1]) << 8)
        output.extend((
            _BASE64_ALPHABET[(code >> 18) & 63],
            _BASE64_ALPHABET[(code >> 12) & 63],
            _BASE64_ALPHABET[(code >> 6) & 63],
            _PAD,
        ))
    return "".join(output)
