from typing import List, Dict
from pydantic import ConfigDict, SecretStr
from pydantic_settings import BaseSettings

DEFAULT_ENV_FILE = ".env"

class Settings(BaseSettings):
    db_url: SecretStr = SecretStr(
        "postgresql://persister:PASSWD@journaldb.electricity.works/journaldb"
    )
    db_url_no_async: SecretStr = SecretStr(
        "postgresql://persister:PASSWD@journaldb.electricity.works/journaldb"
    )
    gbo_db_url: SecretStr = SecretStr(
        "postgresql://backofficedb:PASSWD@backofficedb.electricity.works/backofficedb"
    )
    gbo_db_url_no_async: SecretStr = SecretStr(
        "postgresql://backofficedb:PASSWD@backofficedb.electricity.works/backofficedb"
    )
    secret_key: SecretStr = SecretStr("secret_key")
    api_password: SecretStr = SecretStr("ThermostatAPIKey")
    running_locally: bool = False
    google_maps_api_key: SecretStr = SecretStr("")

    model_config = ConfigDict(
        env_prefix="backend_",
        env_nested_delimiter="__",
        extra="ignore",
    )