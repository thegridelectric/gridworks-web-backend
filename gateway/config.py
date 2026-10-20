from urllib.parse import quote_plus

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

GATEWAY_HOST = "0.0.0.0"
GATEWAY_PORT = 8100

# The tap broker's fleet-stream exchange, the binding that selects every
# scada message on it, and the prefix the gateway's login may declare
# queues under.
EAR_TX = "ear_tx"
FLEET_BINDING_KEY = "gw.#"
QUEUE_PREFIX = "web.gateway."


class GatewaySettings(BaseSettings):
    rabbit_user: str
    rabbit_password: SecretStr
    rabbit_host: str
    rabbit_port: int
    rabbit_vhost: str

    model_config = SettingsConfigDict(
        env_prefix="backend_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @property
    def rabbit_url(self) -> SecretStr:
        pw = quote_plus(self.rabbit_password.get_secret_value())
        return SecretStr(
            f"amqps://{self.rabbit_user}:{pw}@{self.rabbit_host}:{self.rabbit_port}/{self.rabbit_vhost}"
        )
