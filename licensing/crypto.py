"""授权码编解码与签名校验。

授权码格式：
    LEDGER1-<base32( 2字节长度 + payload(JSON) + ed25519签名 )>，按 6 字符一组用 - 分隔

payload 示例：
    {"e": "pro", "exp": "20271029", "m": "A1B2C3D4E5F6", "s": 10}
    exp 为到期日 YYYYMMDD；"0" 或不传表示永久；m 为空表示不绑定机器。
"""
import base64
import json

from cryptography.exceptions import InvalidSignature

from .keys import public_key

PREFIX = 'LEDGER1'
GROUP = 6


def _b32e(data: bytes) -> str:
    return base64.b32encode(data).decode().rstrip('=')


def _b32d(text: str) -> bytes:
    text = text.upper().replace('-', '').replace(' ', '')
    if text.startswith(PREFIX):
        text = text[len(PREFIX):]
    text += '=' * ((8 - len(text) % 8) % 8)
    return base64.b32decode(text)


def sign_code(payload: dict, private_key) -> str:
    """用私钥签发授权码。"""
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True, ensure_ascii=False).encode('utf-8')
    sig = private_key.sign(body)
    blob = len(body).to_bytes(2, 'big') + body + sig
    raw = _b32e(blob)
    groups = [raw[i:i + GROUP] for i in range(0, len(raw), GROUP)]
    return PREFIX + '-' + '-'.join(groups)


def verify_code(code: str) -> dict:
    """校验授权码，返回 payload；签名或格式不合法一律返回 None。"""
    if not code:
        return None
    try:
        raw = _b32d(code)
        n = int.from_bytes(raw[:2], 'big')
        body, sig = raw[2:2 + n], raw[2 + n:]
        if len(body) != n or not sig:
            return None
        public_key().verify(sig, body)
        payload = json.loads(body.decode('utf-8'))
        return payload if isinstance(payload, dict) else None
    except (InvalidSignature, ValueError, TypeError, IndexError, UnicodeDecodeError):
        return None
