from datetime import datetime, timedelta, timezone
import os
import threading
from typing import Any

import dotenv
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from gw_data.db.models import UserSql

from api.config import Settings

settings = Settings(_env_file=dotenv.find_dotenv())
access_token_secret = settings.access_token_secret.get_secret_value()


ALGORITHM = "HS256"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v2/sessions")

async_session_maker = None
async_session_maker_lock = threading.Lock()

def get_sessionmaker():
    global async_session_maker
    if async_session_maker is None:
        with async_session_maker_lock:
            if async_session_maker is None:
                url = settings.tsdb_url.get_secret_value()
                if not url:
                    raise ValueError('VIS_DB2_URL env variable is undefined')

                engine = create_async_engine(url, pool_pre_ping=True, echo=True)
                async_session_maker = async_sessionmaker(bind=engine, expire_on_commit=False, autocommit=False, autoflush=False)

    return async_session_maker

async def get_db():
    async_session_maker = get_sessionmaker()
    async with async_session_maker() as async_session:
        try:
            yield async_session
        finally:
            await async_session.close()

ACCESS_TOKEN_EXPIRE_MINUTES = 7 * 24 * 60

def encode_token(username) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "exp": expires}, key = access_token_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, key = access_token_secret, algorithms=[ALGORITHM])

async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> UserSql:
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        username: str | None = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    db_result = await db.execute(select(UserSql).where(UserSql.username == username))
    user = db_result.unique().scalar_one_or_none()
    if user is None or not user.is_active:
        raise credentials_exception
    return user