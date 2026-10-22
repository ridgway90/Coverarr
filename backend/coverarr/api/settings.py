import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.db import get_session
from coverarr.services.settings_store import PUBLIC_KEYS, SECRET_KEYS, get_setting, set_setting

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def read_settings(session: AsyncSession = Depends(get_session)):
    out: dict[str, Any] = {}
    for key in sorted(PUBLIC_KEYS):
        value = await get_setting(session, key)
        if key in SECRET_KEYS:
            out[key] = {"has_value": bool(value)}
        else:
            out[key] = value
    return out


@router.put("")
async def update_settings(body: dict[str, Any], session: AsyncSession = Depends(get_session)):
    unknown = set(body) - PUBLIC_KEYS
    if unknown:
        raise HTTPException(422, f"Unknown settings: {', '.join(sorted(unknown))}")
    for key, value in body.items():
        if key in SECRET_KEYS and value is None:
            continue  # leave unchanged
        if key == "poster_match_threshold":
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(422, "poster_match_threshold must be a number") from exc
            if not 0 <= value <= 32:
                raise HTTPException(422, "poster_match_threshold must be between 0 and 32")
        elif key == "server_load":
            if value not in ("gentle", "normal", "fast"):
                raise HTTPException(422, "server_load must be gentle, normal or fast")
        elif key == "watch_region":
            value = str(value or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", value):
                raise HTTPException(422, "The streaming region is a country code, e.g. GB")
        elif isinstance(value, str):
            value = value.strip()
        await set_setting(session, key, value)
    await session.commit()
    return await read_settings(session)
