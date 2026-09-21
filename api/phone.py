from __future__ import annotations

import secrets
import socket
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/phone", tags=["phone"])

_active_pairing_codes: dict[str, datetime] = {}
_PAIRING_CODE_EXPIRY_MINUTES = 15


class PhoneAccessConfig(BaseModel):
    enabled: bool
    lan_url: str | None
    pairing_code: str | None
    expires_at: str | None


def _get_lan_ip() -> str | None:
    """Get the primary LAN IPv4 address for this machine."""
    try:
        test_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        test_socket.settimeout(0.1)
        # Connect to a public DNS server (doesn't actually send data)
        test_socket.connect(("8.8.8.8", 80))
        lan_ip = test_socket.getsockname()[0]
        test_socket.close()
        return lan_ip
    except Exception:
        return None


def _generate_pairing_code() -> str:
    """Generate a 6-character alphanumeric pairing code."""
    return secrets.token_urlsafe(6)[:6].upper()


def _validate_pairing_code(code: str) -> bool:
    """Check if a pairing code is valid and not expired."""
    if code not in _active_pairing_codes:
        return False
    expiry = _active_pairing_codes[code]
    if datetime.now(timezone.utc) > expiry:
        del _active_pairing_codes[code]
        return False
    return True


@router.post("/enable", response_model=PhoneAccessConfig)
def enable_phone_access(request: Request) -> PhoneAccessConfig:
    """Enable phone access and generate a pairing code."""
    lan_ip = _get_lan_ip()
    if not lan_ip:
        raise HTTPException(
            status_code=500,
            detail="Could not determine LAN IP address. Ensure you are connected to a network."
        )

    port = request.app.state.settings.server.port
    lan_url = f"http://{lan_ip}:{port}/phone"

    pairing_code = _generate_pairing_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=_PAIRING_CODE_EXPIRY_MINUTES)
    _active_pairing_codes[pairing_code] = expires_at

    # Clean up expired codes
    now = datetime.now(timezone.utc)
    expired = [code for code, expiry in _active_pairing_codes.items() if now > expiry]
    for code in expired:
        del _active_pairing_codes[code]

    return PhoneAccessConfig(
        enabled=True,
        lan_url=lan_url,
        pairing_code=pairing_code,
        expires_at=expires_at.isoformat()
    )


@router.post("/disable")
def disable_phone_access() -> dict[str, str]:
    """Disable phone access and clear all pairing codes."""
    _active_pairing_codes.clear()
    return {"status": "disabled"}


@router.get("/status", response_model=PhoneAccessConfig)
def get_phone_access_status(request: Request) -> PhoneAccessConfig:
    """Get current phone access configuration."""
    if not _active_pairing_codes:
        return PhoneAccessConfig(
            enabled=False,
            lan_url=None,
            pairing_code=None,
            expires_at=None
        )

    lan_ip = _get_lan_ip()
    port = request.app.state.settings.server.port
    lan_url = f"http://{lan_ip}:{port}/phone" if lan_ip else None

    # Return the first valid code
    now = datetime.now(timezone.utc)
    for code, expiry in list(_active_pairing_codes.items()):
        if now <= expiry:
            return PhoneAccessConfig(
                enabled=True,
                lan_url=lan_url,
                pairing_code=code,
                expires_at=expiry.isoformat()
            )

    return PhoneAccessConfig(
        enabled=False,
        lan_url=None,
        pairing_code=None,
        expires_at=None
    )


@router.get("/validate")
def validate_pairing(code: str) -> dict[str, bool]:
    """Validate a pairing code."""
    if len(code) != 6:
        return {"valid": False}
    return {"valid": _validate_pairing_code(code.upper())}
