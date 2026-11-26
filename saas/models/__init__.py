"""Every SaaS table registers on saas.db.Base by being imported here.

Each module is owned by one agent (docs/saas/CONTRACT.md). Cross-module references use string
foreign keys ("users.id"), never imports of another agent's model classes at module import time.
"""
from . import core, billing, jobs, tracking, security, waitlist  # noqa: F401
