"""saas.waitlist - see docs/saas/CONTRACT.md for ownership.

`redeem_invite(db, code, email) -> WaitlistEntry | None` is the contract entry point for the core
agent's signup (imported lazily so importing the package never drags in the models).
"""
from __future__ import annotations


def redeem_invite(db, code: str, email: str):
    from .service import redeem_invite as _redeem

    return _redeem(db, code, email)
