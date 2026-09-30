"""PostgreSQL 连接辅助。"""

from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://timer:timer@db:5432/race_timer",
)


def connect() -> psycopg.Connection:
    """打开一个以字典行返回结果的数据库连接。"""
    conn = psycopg.connect(DSN, row_factory=dict_row, autocommit=False)
    return conn


def wait_for_database(retries: int = 30, delay: float = 1.0) -> None:
    """等待数据库可连接（供容器启动时使用）。"""
    import time

    last_error: Exception | None = None
    for _ in range(retries):
        try:
            with psycopg.connect(DSN, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:  # pragma: no cover - 启动期重试
            last_error = exc
            time.sleep(delay)
    raise RuntimeError(f"数据库不可用: {last_error}")
