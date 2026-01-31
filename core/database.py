import mysql.connector
from mysql.connector import pooling
from contextlib import contextmanager
from core.config import settings


class Database:

    _pool: pooling.MySQLConnectionPool | None = None

    @classmethod
    def initialize(cls) -> None:
        if cls._pool is not None:
            return

        cls._pool = pooling.MySQLConnectionPool(
            pool_name="cpl_pool",
            pool_size=settings.DB_POOL_SIZE,
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.DB_NAME,
            autocommit=False
        )

    @classmethod
    def get_connection(cls):
        if cls._pool is None:
            raise RuntimeError("Database pool is not initialized.")
        return cls._pool.get_connection()


@contextmanager
def get_db():
    connection = Database.get_connection()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()