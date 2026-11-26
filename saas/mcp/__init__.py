"""Remote MCP server (core agent). `mount(app)` is called by saas.main.create_app.

It also installs the core error envelope handler (saas.auth.errors.install) app-wide, because this is
the one app-level hook the core owns; the integrator may move that call into create_app.
"""
from fastapi import FastAPI


def mount(app: FastAPI) -> None:
    from ..auth import errors
    from .server import mount as mount_mcp

    errors.install(app)
    mount_mcp(app)
