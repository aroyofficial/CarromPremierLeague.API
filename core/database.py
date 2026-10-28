import re
from typing import Any

import pyodbc
from core.config import settings


def _sql_server_query(query: str) -> str:
    normalized = query.replace("`Order`", "[Order]").replace("%s", "?")
    normalized = re.sub(r"\bIFNULL\s*\(", "ISNULL(", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bCURDATE\s*\(\)", "CAST(GETDATE() AS DATE)", normalized, flags=re.IGNORECASE)

    # The legacy repositories only use LIMIT 1 for existence checks or an
    # ordered single-row lookup. SQL Server requires TOP (1) instead.
    normalized = re.sub(
        r"SELECT\s+1\s+FROM\s+(.+?)\s+LIMIT\s+1",
        r"SELECT TOP (1) 1 FROM \1",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    normalized = re.sub(
        r"(ORDER\s+BY\s+.+?)\s+LIMIT\s+1",
        r"\1",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return normalized


class SqlServerCursor:
    def __init__(self, cursor: pyodbc.Cursor):
        self._cursor = cursor
        self.lastrowid: int | None = None

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def execute(self, query: str, params: tuple[Any, ...] | list[Any] | None = None):
        query = _sql_server_query(query)
        values = tuple(params or ())
        self._cursor.execute(query, values)

        if query.lstrip().upper().startswith("INSERT"):
            self._cursor.execute("SELECT CAST(SCOPE_IDENTITY() AS BIGINT) AS Id")
            row = self._cursor.fetchone()
            self.lastrowid = int(row[0]) if row and row[0] is not None else None
        return self

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return dict(zip([column[0] for column in self._cursor.description], row))

    def fetchall(self) -> list[dict[str, Any]]:
        rows = self._cursor.fetchall()
        if not self._cursor.description:
            return []
        columns = [column[0] for column in self._cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    def nextset(self) -> bool:
        return bool(self._cursor.nextset())

    def close(self) -> None:
        self._cursor.close()


class SqlServerConnection:
    def __init__(self, connection: pyodbc.Connection):
        self._connection = connection

    def cursor(self, dictionary: bool = False) -> SqlServerCursor:
        return SqlServerCursor(self._connection.cursor())

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class Database:

    @classmethod
    def initialize(cls) -> None:
        # Connections are opened per request. SQL Server ODBC pooling is
        # enabled by pyodbc and configured by the installed ODBC driver.
        return None

    @classmethod
    def get_connection(cls) -> SqlServerConnection:
        connection_string = (
            "DRIVER={ODBC Driver 17 for SQL Server};"
            f"SERVER={settings.DB_HOST},{settings.DB_PORT};"
            f"DATABASE={settings.DB_NAME};"
            f"UID={settings.DB_USER};PWD={settings.DB_PASSWORD};"
            "TrustServerCertificate=yes;"
        )
        return SqlServerConnection(pyodbc.connect(connection_string, autocommit=False))


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