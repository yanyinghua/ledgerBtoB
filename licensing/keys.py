"""授权公钥。

授权码由卖方用 ed25519 私钥签名，本文件内置对应公钥用于校验。
私钥不进仓库（保存在卖方机器 ~/.ledger-license/private.key，权限 600）。
公钥硬编码而非放配置：买方改配置就能绕过，放代码里至少要多一步反编译/改字节码。
"""
import base64
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# 产品公钥（base64，32 字节 ed25519）
PUBLIC_KEY_B64 = 'PX9jUcepodntxR1DxuMRpntBVahzTlfAJOXm36iW6DA='

# 仅用于开发自测：可用环境变量临时替换公钥，生产部署不要设置。
_OVERRIDE = os.environ.get('LEDGER_LICENSE_PUBLIC_KEY', '').strip()


def public_key():
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(_OVERRIDE or PUBLIC_KEY_B64))
