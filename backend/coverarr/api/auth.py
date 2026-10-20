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
    user = User(username=body.username.strip(), password_hash=hash_password(body.password))
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
    user = await session.scalar(select(User).where(User.username == body.username.strip()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    _set_cookie(request, response, user)
    return {"username": user.username}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}
