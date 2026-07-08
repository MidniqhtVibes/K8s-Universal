from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://clusterbuilder:clusterbuilder@postgres/clusterbuilder"
    master_key: SecretStr
    session_secret: SecretStr
    initial_admin_password: SecretStr
    data_root: Path = Path("/data")
    source_root: Path = Path("/workspace")
    session_https_only: bool = False
    worker_poll_seconds: float = Field(default=2.0, ge=0.2)
    ssh_wait_timeout: int = Field(default=600, ge=30)
    terraform_parallelism: int = Field(default=4, ge=1, le=20)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
