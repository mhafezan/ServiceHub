"""Load validated environment configuration without embedding deployment secrets."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from servicehub.core.paths import REPOSITORY_ROOT


class Settings(BaseSettings):
    """Keep external integrations optional locally and fail closed in deployed environments."""

    model_config = SettingsConfigDict(env_file=REPOSITORY_ROOT / ".env", extra="ignore")
    environment: str = "local"
    database_url: str = "sqlite:///servicehub.db"
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_channel_id: str = ""
    telegram_channel_url: str = ""
    telegram_webhook_secret: str = ""
    public_url: str = "http://localhost:8000"
    session_secret: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    google_places_api_key: str = ""
    operator_ids: list[int] = []
    task_mode: str = "local"
    gcp_project: str = ""
    gcp_location: str = "northamerica-northeast2"
    task_queue: str = "servicehub"
    worker_url: str = ""
    worker_service_account: str = ""
    internal_audience: str = ""
    runtime_role: str = "api"

    def validate_deployment(self) -> None:
        """Reject production startup with missing credentials or an unsafe database."""
        if self.environment != "local":
            required = (
                self.telegram_bot_token, self.telegram_webhook_secret,
                self.session_secret, self.telegram_channel_id,
                self.google_places_api_key, self.worker_service_account,
            )
            if not all(required) or not self.database_url.startswith("mysql"):
                raise ValueError("Deployment requires MySQL and configured integration secrets")
        if self.session_secret and len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 random characters")


@lru_cache
def settings() -> Settings:
    """Return the process configuration once to keep request behavior consistent."""
    result = Settings()
    result.validate_deployment()
    return result
