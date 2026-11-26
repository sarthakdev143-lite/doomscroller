"""TOTP 2FA (RFC 6238 via pyotp) and single-use recovery codes.

* The seed is AES-GCM sealed at rest (crypto.seal) with a key derived from SAAS_SECRET_KEY.
* A code is accepted for the current 30 s step and one step either side, and each step only once:
  `last_step` is advanced with a guarded UPDATE, so a code observed over a shoulder (or replayed by
  a racing request) cannot be used again.
* Recovery codes are 10 random codes, stored as SHA-256, consumed with a guarded UPDATE.
"""
from __future__ import annotations

import base64
import hmac
import secrets
import time
from typing import Optional

import pyotp
import segno
from sqlalchemy import update
from sqlalchemy.orm import Session

from ..common import utcnow
from ..config import settings
from ..models.core import RecoveryCode, TotpSecret, User
from .crypto import seal, sha256_hex, unseal

_PURPOSE = "totp"
RECOVERY_COUNT = 10


def _normalise_code(code: str) -> str:
    return "".join(ch for ch in (code or "") if ch.isalnum()).upper()


def begin_setup(db: Session, user: User) -> dict:
    """Create (or replace an unconfirmed) secret; return what the setup page shows."""
    row = db.get(TotpSecret, user.id)
    if row is not None and row.confirmed_at is not None:
        raise ValueError("already_enabled")
    secret = pyotp.random_base32(32)
    if row is None:
        row = TotpSecret(user_id=user.id, secret_enc=seal(_PURPOSE, secret))
        db.add(row)
    else:
        row.secret_enc = seal(_PURPOSE, secret)
        row.created_at = utcnow()
        row.last_step = 0
    db.flush()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=settings.brand_name)
    qr = segno.make(uri, error="m")
    svg = qr.svg_inline(scale=5, dark="#111111", light="#ffffff")
    data_uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": svg, "qr_data_uri": data_uri}


def is_enabled(db: Session, user_id: str) -> bool:
    row = db.get(TotpSecret, user_id)
    return row is not None and row.confirmed_at is not None


def _check(db: Session, row: TotpSecret, code: str, *, at: Optional[float] = None) -> bool:
    code = _normalise_code(code)
    if len(code) != 6 or not code.isdigit():
        return False
    secret = unseal(_PURPOSE, row.secret_enc)
    totp = pyotp.TOTP(secret)
    now = time.time() if at is None else at
    current = int(now // totp.interval)
    for step in (current - 1, current, current + 1):
        if step <= row.last_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            res = db.execute(update(TotpSecret)
                             .where(TotpSecret.user_id == row.user_id, TotpSecret.last_step < step)
                             .values(last_step=step))
            if res.rowcount == 1:
                row.last_step = step
                return True
            return False
    return False


def confirm_setup(db: Session, user: User, code: str) -> Optional[list[str]]:
    """Turn 2FA on once the user proves the app works; returns fresh recovery codes."""
    row = db.get(TotpSecret, user.id)
    if row is None or row.confirmed_at is not None:
        return None
    if not _check(db, row, code):
        return None
    row.confirmed_at = utcnow()
    db.flush()
    return regenerate_recovery_codes(db, user)


def verify(db: Session, user: User, code: str) -> bool:
    """A TOTP code or, failing that, a recovery code."""
    row = db.get(TotpSecret, user.id)
    if row is None or row.confirmed_at is None:
        return False
    if _check(db, row, code):
        return True
    return use_recovery_code(db, user, code)


def disable(db: Session, user: User) -> None:
    row = db.get(TotpSecret, user.id)
    if row is not None:
        db.delete(row)
    db.query(RecoveryCode).filter(RecoveryCode.user_id == user.id).delete()
    db.flush()


def _new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
    raw = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def regenerate_recovery_codes(db: Session, user: User) -> list[str]:
    db.query(RecoveryCode).filter(RecoveryCode.user_id == user.id).delete()
    codes = [_new_code() for _ in range(RECOVERY_COUNT)]
    for c in codes:
        db.add(RecoveryCode(user_id=user.id, code_hash=sha256_hex(_normalise_code(c))))
    db.flush()
    return codes


def use_recovery_code(db: Session, user: User, code: str) -> bool:
    norm = _normalise_code(code)
    if len(norm) != 10:
        return False
    row = (db.query(RecoveryCode)
           .filter(RecoveryCode.user_id == user.id, RecoveryCode.code_hash == sha256_hex(norm),
                   RecoveryCode.used_at.is_(None))
           .one_or_none())
    if row is None:
        return False
    res = db.execute(update(RecoveryCode).where(RecoveryCode.id == row.id, RecoveryCode.used_at.is_(None))
                     .values(used_at=utcnow()))
    return res.rowcount == 1


def remaining_recovery_codes(db: Session, user_id: str) -> int:
    return db.query(RecoveryCode).filter(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None)).count()
