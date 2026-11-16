"""Transactional, versioned SQLite migrations using only the standard library."""

from __future__ import annotations

import sqlite3

from . import v001_forms

LATEST_VERSION = 1


class MigrationError(RuntimeError):
    """Refuse migrations that cannot safely preserve the database."""


def _begin(conn: sqlite3.Connection) -> int:
    if conn.in_transaction:
        raise MigrationError("未確定の変更があります。先に commit または rollback してください")
    conn.execute("BEGIN IMMEDIATE")
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version not in (0, LATEST_VERSION):
        conn.rollback()
        raise MigrationError(f"未対応のDBスキーマ版です: {version}")
    return version


def _script(conn: sqlite3.Connection, sql: str) -> None:
    # executescript commits implicitly, so execute trusted statements individually.
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise MigrationError("マイグレーションSQLが完結していません")


def upgrade(conn: sqlite3.Connection, base_schema: str) -> None:
    """Initialize the legacy schema and upgrade unversioned or older databases."""
    version = _begin(conn)
    try:
        _script(conn, base_schema)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(companies)")}
        if "refusal_confirmed" not in columns:
            conn.execute("ALTER TABLE companies ADD COLUMN refusal_confirmed TEXT NOT NULL DEFAULT ''")
        if version < 1:
            v001_forms.upgrade(conn)
            conn.execute("PRAGMA user_version=1")
        v001_forms.validate(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise MigrationError("既存データの参照整合性を確認できません。DBは変更していません")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def downgrade(conn: sqlite3.Connection) -> None:
    """Return an empty test database to version 0; never discard operational data."""
    version = _begin(conn)
    try:
        if version == 1:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                if conn.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone() is not None:
                    raise MigrationError("downgrade は空のテストDBだけで実行できます。既存データは削除しません")
            v001_forms.downgrade(conn)
            conn.execute("PRAGMA user_version=0")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
