from urllib.parse import quote_plus

from pydantic import ConfigDict, SecretStr
from pydantic_settings import BaseSettings

DEFAULT_ENV_FILE = ".env"

CORS_ORIGINS = [
    "https://thegridelectric.github.io",
    "https://web-backend.electricity.works",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

JOURNAL_DB_USER = "journaldb"
JOURNAL_DB_HOST = "localhost"
JOURNAL_DB_NAME = "journaldb"

BACKOFFICE_DB_USER = "backofficedb"
BACKOFFICE_DB_HOST = "localhost"
BACKOFFICE_DB_NAME = "backofficedb"


def _postgres_url(
    user: str,
    password: SecretStr,
    host: str,
    db: str,
    *,
    async_driver: bool,
) -> str:
    scheme = "postgresql+asyncpg" if async_driver else "postgresql"
    pw = quote_plus(password.get_secret_value())
    return f"{scheme}://{user}:{pw}@{host}/{db}"


class Settings(BaseSettings):
    journal_db_password: SecretStr
    backoffice_db_password: SecretStr
    tsdb_url: SecretStr
    access_token_secret: SecretStr
    running_locally: bool = False
    google_maps_api_key: SecretStr = SecretStr("")
    alert_manager_url: str = "http://localhost:8000"
    alert_manager_token: SecretStr = SecretStr("")

    model_config = ConfigDict(
        env_prefix="backend_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # @property
    # def tsdb_url(self) -> SecretStr:
    #     return SecretStr(
    #         self.tsdb_url
    #     )

    @property
    def journaldb_url_async(self) -> SecretStr:
        return SecretStr(
            _postgres_url(
                JOURNAL_DB_USER,
                self.journal_db_password,
                JOURNAL_DB_HOST,
                JOURNAL_DB_NAME,
                async_driver=True,
            )
        )

    @property
    def journaldb_url(self) -> SecretStr:
        return SecretStr(
            _postgres_url(
                JOURNAL_DB_USER,
                self.journal_db_password,
                JOURNAL_DB_HOST,
                JOURNAL_DB_NAME,
                async_driver=False,
            )
        )

    @property
    def backofficedb_url_async(self) -> SecretStr:
        return SecretStr(
            _postgres_url(
                BACKOFFICE_DB_USER,
                self.backoffice_db_password,
                BACKOFFICE_DB_HOST,
                BACKOFFICE_DB_NAME,
                async_driver=True,
            )
        )

    @property
    def backofficedb_url(self) -> SecretStr:
        return SecretStr(
            _postgres_url(
                BACKOFFICE_DB_USER,
                self.backoffice_db_password,
                BACKOFFICE_DB_HOST,
                BACKOFFICE_DB_NAME,
                async_driver=False,
            )
        )
