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

        self.DB_HOST: str = self._get_env("CPL_DB_HOST")
        self.DB_PORT: int = self._get_int_env("CPL_DB_PORT")
        self.DB_USER: str = self._get_env("CPL_DB_USER")
        self.DB_PASSWORD: str = self._get_env("CPL_DB_PWD")
        self.DB_NAME: str = self._get_env("CPL_DB_NAME")
        self.DB_POOL_SIZE: int = self._get_int_env("CPL_DB_POOL_SIZE")

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