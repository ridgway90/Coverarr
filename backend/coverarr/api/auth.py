import asyncio
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverarr.db import get_session
from coverarr.models import User
from coverarr.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    _serializer,
    hash_password,
    make_session_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Failed logins per address: after MAX_FAILURES within FAILURE_WINDOW, wait it out.
MAX_FAILURES = 10
FAILURE_WINDOW = 15 * 60
_failures: dict[str, deque[float]] = {}
# Checked against when the username doesn't exist, so that takes as long as a wrong password.
_DUMMY_HASH = hash_password("coverarr-not-a-user")
# Each check is ~60 ms of CPU; a few at a time leaves the rest of the app responsive.
_checks = asyncio.Semaphore(2)


def _recent_failures(address: str, now: float) -> deque[float]:
    times = _failures.setdefault(address, deque())
    while times and times[0] < now - FAILURE_WINDOW:
        times.popleft()
    return times


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class SetupRequest(Credentials):
    password: str = Field(min_length=8, max_length=200)


def _set_cookie(request: Request, response: Response, user: User) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        make_session_token(request, user),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


@router.get("/status")
async def auth_status(request: Request, session: AsyncSession = Depends(get_session)):
    users = await session.scalar(select(func.count(User.id)))
    username = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            data = _serializer(request).loads(token, max_age=SESSION_MAX_AGE)
            user = await session.get(User, data.get("uid"))
            username = user.username if user else None
        except Exception:
            username = None
    return {
        "setup_required": users == 0,
        "authenticated": username is not None,
        "username": username,
    }


@router.post("/setup")
async def setup(
    body: SetupRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    if await session.scalar(select(func.count(User.id))):
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup already completed")
    password_hash = await asyncio.to_thread(hash_password, body.password)
    user = User(username=body.username.strip(), password_hash=password_hash)
    session.add(user)
    await session.commit()
    _set_cookie(request, response, user)
    return {"username": user.username}


@router.post("/login")
async def login(
    body: Credentials,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    address = request.client.host if request.client else "?"
    now = time.monotonic()
    failures = _recent_failures(address, now)
    if len(failures) >= MAX_FAILURES:
        wait = int(failures[0] + FAILURE_WINDOW - now) // 60 + 1
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many failed sign-ins; try again in {wait} minute{'s' if wait != 1 else ''}",
        )
    user = await session.scalar(select(User).where(User.username == body.username.strip()))
    async with _checks:
        ok = await asyncio.to_thread(
            verify_password, body.password, user.password_hash if user else _DUMMY_HASH
        )
    if user is None or not ok:
        failures.append(now)
        if len(_failures) > 10_000:  # don't grow forever under a spray of addresses
            _failures.clear()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    _failures.pop(address, None)
    _set_cookie(request, response, user)
    return {"username": user.username}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}
