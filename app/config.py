# app/config.py
"""Типизированная конфигурация: baseline, приватный .env, затем окружение."""

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Проверяет тайм-ауты и единственную последовательную обработку."""

    model_config = SettingsConfigDict(
        env_file=(".env.example", ".env"), extra="ignore", case_sensitive=False
    )
    database_url: SecretStr = SecretStr("")
    postgres_host: str = "leaders-parser-postgres"
    postgres_db: str = "leaders_parser"
    postgres_user: str = "leaders_parser"
    postgres_password: SecretStr = SecretStr("")
    source_url: str = "https://leaders-project.ru/companies/projecting?size=100"
    user_agent: str = "LeadersProjectParser/0.1 (+public company catalogue)"
    source_mode: str = "auto"
    source_api_access_approved: bool = True
    request_min_delay: float = Field(7, ge=0)
    request_max_delay: float = Field(12, ge=0)
    http_connect_timeout: float = Field(10, gt=0)
    http_read_timeout: float = Field(30, gt=0)
    http_pool_timeout: float = Field(10, gt=0)
    http_total_timeout: float = Field(45, gt=0)
    max_retries: int = Field(3, ge=0, le=5)
    max_concurrency: int = Field(1, ge=1, le=1)
    rate_limit_cooldown: float = Field(1800, ge=0)
    status_poll_interval: float = Field(5, ge=1)
    browser_render_timeout: float = Field(1800, gt=0)
    retention_days: int = Field(7, ge=1)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_limits(self):
        """Отклоняет несогласованные задержки и неизвестный режим загрузки."""
        if self.request_max_delay < self.request_min_delay:
            raise ValueError("REQUEST_MAX_DELAY must be >= REQUEST_MIN_DELAY")
        if self.source_mode not in {"auto", "http", "browser"}:
            raise ValueError("SOURCE_MODE must be auto, http or browser")
        return self

    def db_url(self) -> str:
        """Собирает URL с корректным кодированием пароля, не выводя его в логи."""
        from urllib.parse import quote

        if self.database_url.get_secret_value():
            return self.database_url.get_secret_value()
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:5432/{self.postgres_db}"
        )
