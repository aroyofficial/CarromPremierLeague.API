import os
from functools import lru_cache
from typing import Literal


class Settings:

    def __init__(self):
        self.APP_NAME: str = self._get_env("CPL_APP_NAME")
        self.APP_ENV: Literal["dev", "stage", "prod"] = self._get_env("CPL_APP_ENV")  # type: ignore
        self.APP_HOST: str = self._get_env("CPL_APP_HOST")
        self.APP_PORT: int = self._get_int_env("CPL_APP_PORT")
        self.APP_ALLOWED_HOSTS: list[str] = self._get_env("CPL_APP_ALLOWED_HOSTS").split(",")

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