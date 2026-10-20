from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gw_data.db.models import GNodeSql, InstallationSql, UserInstallationRoleSql, UserSql


async def verify_data_access(db: AsyncSession, username: str, installation_id: str, effective_date: datetime):
    # Users with role owner or admin on the installation (or on the "null" installation)
    # can view anything. All others can only view data that is older than 10 days.

    roles_query = select(UserInstallationRoleSql).join(UserSql).outerjoin(InstallationSql).outerjoin(GNodeSql).filter(
        UserSql.username == username, 
        or_(
            GNodeSql.alias.is_(None),
            GNodeSql.alias == installation_id
        )
    )

    result = await db.execute(roles_query)
    roles = result.scalars().all()

    if len(roles) == 0:
        raise HTTPException(status_code=404)

    is_owner_admin = any(r.role in ['admin', 'owner'] for r in roles)
    is_old_data = (datetime.now(tz=UTC) - timedelta(days=10)) > effective_date

    if not (is_owner_admin or is_old_data):
        raise HTTPException(status_code=404)
    