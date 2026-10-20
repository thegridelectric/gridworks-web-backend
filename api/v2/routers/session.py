from typing import Any
import uuid
from datetime import datetime, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gw_data.db.models import UserSql
from sqlalchemy.orm import selectinload

from ..dependencies import encode_token, get_db

router = APIRouter()

# This is not a SEMA model because it's part of the OAuth2 standard
class SessionToken(BaseModel):
    access_token: str
    token_type: str


def verify_password(plain_password, hashed_password):
    if isinstance(hashed_password, str):
        hashed_password = hashed_password.encode("utf-8")
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password)


@router.post("/api/v2/sessions", response_model=SessionToken)
async def create_session(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    db_result = await db.execute(
        select(UserSql).options(selectinload(UserSql.installation_roles)).where(UserSql.username == form_data.username)
    )
    user = db_result.unique().scalar_one_or_none()

    if user is None or not user.is_active or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Update last_login without resetting updated_at
    update_last_login = update(UserSql).values(last_login=datetime.now(timezone.utc), updated_at=user.updated_at)
    await db.execute(update_last_login)
    await db.commit()

    sys_admin_roles = [r for r in user.installation_roles if r.role == 'admin' and r.installation_id is None]
    token = encode_token(user.username, len(sys_admin_roles) > 0)
    return SessionToken(access_token=token, token_type="bearer")
