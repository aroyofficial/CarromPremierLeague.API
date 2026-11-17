import os
from functools import lru_cache
from typing import Literal


def _load_local_env() -> None:
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


class Settings:

    def __init__(self):
        self.APP_NAME: str = self._get_env("CPL_APP_NAME")
        self.APP_ENV: Literal["dev", "stage", "prod"] = self._get_env("CPL_APP_ENV")  # type: ignore
        self.APP_HOST: str = self._get_env("CPL_APP_HOST")
        self.APP_PORT: int = self._get_int_env("CPL_APP_PORT")
        self.APP_ALLOWED_HOSTS: list[str] = self._get_env("CPL_APP_ALLOWED_HOSTS").split(",")
        self.AUTH_JWT_SECRET: str = self._get_env("CPL_AUTH_JWT_SECRET")
        self.AUTH_TOKEN_EXPIRE_MINUTES: int = self._get_int_env("CPL_AUTH_TOKEN_EXPIRE_MINUTES")
        # off (default) | dev (APP_ENV=dev only). No production SMS provider is configured.
        self.PHONE_OTP_PROVIDER: str = (self._get_optional_env("CPL_PHONE_OTP_PROVIDER") or "off").lower()
        # Public season invalidation stream. Off unless explicitly enabled. One process only.
        self.REALTIME_ENABLED: bool = (self._get_optional_env("CPL_REALTIME_ENABLED") or "").lower() in {"1", "true", "yes"}

        self.DB_HOST: str = self._get_env("CPL_DB_HOST")
        self.DB_PORT: int = self._get_int_env("CPL_DB_PORT")
        self.DB_USER: str = self._get_env("CPL_DB_USER")
        self.DB_PASSWORD: str = self._get_env("CPL_DB_PWD")
        self.DB_NAME: str = self._get_env("CPL_DB_NAME")
        self.DB_POOL_SIZE: int = self._get_int_env("CPL_DB_POOL_SIZE")

        self.CLOUDINARY_URL: str | None = self._get_optional_env("CPL_CLOUDINARY_URL")
        self.MEDIA_FOLDER: str = (self._get_optional_env("CPL_MEDIA_FOLDER") or "cpl").strip("/")

        self.GEMINI_API_KEY: str | None = self._get_optional_env("CPL_GEMINI_API_KEY")
        self.GEMINI_MODEL: str = self._get_optional_env("CPL_GEMINI_MODEL") or "gemini-2.5-flash"
        self.GROQ_API_KEY: str | None = self._get_optional_env("CPL_GROQ_API_KEY")
        self.GROQ_MODEL: str = self._get_optional_env("CPL_GROQ_MODEL") or "openai/gpt-oss-120b"
        self.OPENROUTER_API_KEY: str | None = self._get_optional_env("CPL_OPENROUTER_API_KEY")
        self.OPENROUTER_MODEL: str = (
            self._get_optional_env("CPL_OPENROUTER_MODEL") or "meta-llama/llama-3.3-70b-instruct:free"
        )

        self.SMTP_HOST: str | None = self._get_optional_env("CPL_SMTP_HOST")
        self.SMTP_PORT: int = int(self._get_optional_env("CPL_SMTP_PORT") or "587")
        self.SMTP_USERNAME: str | None = self._get_optional_env("CPL_SMTP_USERNAME")
        self.SMTP_PASSWORD: str | None = self._get_optional_env("CPL_SMTP_PASSWORD")
        self.SMTP_FROM: str | None = self._get_optional_env("CPL_SMTP_FROM")
        self.SMTP_FROM_NAME: str = self._get_optional_env("CPL_SMTP_FROM_NAME") or "Carrom Premier League"
        self.SMTP_STARTTLS: bool = (self._get_optional_env("CPL_SMTP_STARTTLS") or "true").lower() != "false"
        self.MAIL_PROVIDER: str = (self._get_optional_env("CPL_MAIL_PROVIDER") or "auto").lower()
        self.MAIL_API_KEY: str | None = self._get_optional_env("CPL_MAIL_API_KEY")
        self.MAIL_FROM: str | None = self._get_optional_env("CPL_MAIL_FROM") or self.SMTP_FROM
        self.GMAIL_CLIENT_ID: str | None = self._get_optional_env("CPL_GMAIL_CLIENT_ID")
        self.GMAIL_CLIENT_SECRET: str | None = self._get_optional_env("CPL_GMAIL_CLIENT_SECRET")
        self.GMAIL_REFRESH_TOKEN: str | None = self._get_optional_env("CPL_GMAIL_REFRESH_TOKEN")
        self.PUBLIC_WEB_URL: str = (
            self._get_optional_env("CPL_PUBLIC_WEB_URL") or "https://aroyofficial.github.io/CarromPremierLeague.Web"
        ).rstrip("/")
        self.MAIL_LOGO_URL: str = self._get_optional_env("CPL_MAIL_LOGO_URL") or (
            "https://lh3.googleusercontent.com/pw/AP1GczM2El9t3olXj05BnAQkgoq03v67a1z09zyEZ-VkUuFSbfsuif4Xv91JwTfaoYNb6"
            "-HvcFLk-j3KN0-9P95DHNissycutEAaFaTJRaO1CQUWcIAUagu_dwsyWesRAVIIL09ZSmPcbpJ_pgOH5t496tSaOQ=w192-h192-s-no-gm"
        )

    @staticmethod
    def _get_optional_env(key: str) -> str | None:
        value = os.getenv(key)
        if value is None or value.strip() == "":
            return None
        return value.strip()

    @staticmethod
    def _get_env(key: str) -> str:
        value = os.getenv(key)
        if value is None or value.strip() == "":
            raise RuntimeError(f"Missing required environment variable: {key}")
        return value

    @staticmethod
    def _get_int_env(key: str) -> int:
        value = os.getenv(key)
        if value is None or value.strip() == "":
            raise RuntimeError(f"Missing required environment variable: {key}")
        try:
            return int(value)
        except ValueError:
            raise RuntimeError(f"Environment variable {key} must be an integer.")


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()