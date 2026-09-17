from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from sqlalchemy import String, create_engine, event, func, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, FolderMap, ItemMap, SyncCursor

_engine = None
_SessionFactory = None


def init_db(db_path: Path) -> None:
    global _engine, _SessionFactory
    # The per-workload thread pools commit concurrently. Allow pooled
    # connections to cross threads (SQLAlchemy hands them to whichever worker
    # asks), wait out writer contention instead of raising "database is
    # locked", and use WAL so readers don't block the single writer.
    _engine = create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    Base.metadata.create_all(_engine)
    _SessionFactory = sessionmaker(bind=_engine)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    if _SessionFactory is None:
        raise RuntimeError("DB not initialised — call init_db() first")
    session: Session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── item_map helpers ──────────────────────────────────────────────────────────

def count_failed_items_since(session: Session, since: datetime) -> int:
    """Failed ItemMap rows touched at/after `since` (naive UTC, matching the
    CURRENT_TIMESTAMP the columns store). Used for CLI exit codes.

    SQLite compares these as strings. CURRENT_TIMESTAMP is "YYYY-MM-DD HH:MM:SS"
    while a bound datetime renders with a ".ffffff" suffix, so a row stamped in
    the same second as the run start would sort *before* it and be missed.
    Compare against a literal in the stored format instead."""
    since_text = since.strftime("%Y-%m-%d %H:%M:%S")
    count: int = session.execute(
        select(func.count())
        .select_from(ItemMap)
        .where(
            ItemMap.status == "failed",
            ItemMap.updated_at >= literal(since_text, String),
        )
    ).scalar_one()
    return count


def is_done(session: Session, user_email: str, workload: str, source_id: str) -> bool:
    row = session.execute(
        select(ItemMap.status)
        .where(
            ItemMap.user_email == user_email,
            ItemMap.workload == workload,
            ItemMap.source_id == source_id,
        )
    ).scalar_one_or_none()
    return row == "done"


def upsert_item(
    session: Session,
    user_email: str,
    workload: str,
    source_id: str,
    *,
    source_hash: str | None = None,
    dest_id: str | None = None,
    status: str = "pending",
    attempts: int = 0,
    last_error: str | None = None,
) -> None:
    stmt = sqlite_insert(ItemMap).values(
        user_email=user_email,
        workload=workload,
        source_id=source_id,
        source_hash=source_hash,
        dest_id=dest_id,
        status=status,
        attempts=attempts,
        last_error=last_error,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_email", "workload", "source_id"],
        set_={
            "source_hash": stmt.excluded.source_hash,
            "dest_id": stmt.excluded.dest_id,
            "status": stmt.excluded.status,
            "attempts": stmt.excluded.attempts,
            "last_error": stmt.excluded.last_error,
            # ON CONFLICT DO UPDATE bypasses Column.onupdate, so stamp it here —
            # count_failed_items_since() (the CLI exit code) keys off this.
            "updated_at": func.now(),
        },
    )
    session.execute(stmt)


class ItemState(NamedTuple):
    status: str
    dest_id: str | None
    source_hash: str | None


def get_item_state(
    session: Session, user_email: str, workload: str, source_id: str
) -> ItemState | None:
    """Status, destination id and source hash of an item, or None if never seen.

    A non-null `dest_id` means the item exists at the destination regardless of
    `status` (an update that failed keeps its dest_id), so callers PATCH it
    rather than creating a duplicate."""
    row = session.execute(
        select(ItemMap.status, ItemMap.dest_id, ItemMap.source_hash).where(
            ItemMap.user_email == user_email,
            ItemMap.workload == workload,
            ItemMap.source_id == source_id,
        )
    ).one_or_none()
    return ItemState(*row) if row else None


def get_dest_id(session: Session, user_email: str, workload: str, source_id: str) -> str | None:
    """dest_id of a successfully migrated item, else None."""
    return session.execute(
        select(ItemMap.dest_id).where(
            ItemMap.user_email == user_email,
            ItemMap.workload == workload,
            ItemMap.source_id == source_id,
            ItemMap.status == "done",
        )
    ).scalar_one_or_none()


def get_item_hash(session: Session, user_email: str, workload: str, source_id: str) -> str | None:
    return session.execute(
        select(ItemMap.source_hash)
        .where(
            ItemMap.user_email == user_email,
            ItemMap.workload == workload,
            ItemMap.source_id == source_id,
        )
    ).scalar_one_or_none()


# ── folder_map helpers ────────────────────────────────────────────────────────

def get_folder_dest(session: Session, user_email: str, workload: str, source_path: str) -> str | None:
    return session.execute(
        select(FolderMap.dest_id)
        .where(
            FolderMap.user_email == user_email,
            FolderMap.workload == workload,
            FolderMap.source_path == source_path,
        )
    ).scalar_one_or_none()


def upsert_folder(
    session: Session,
    user_email: str,
    workload: str,
    source_path: str,
    dest_id: str,
    dest_path: str,
) -> None:
    existing = session.execute(
        select(FolderMap).where(
            FolderMap.user_email == user_email,
            FolderMap.workload == workload,
            FolderMap.source_path == source_path,
        )
    ).scalar_one_or_none()
    if existing:
        existing.dest_id = dest_id
        existing.dest_path = dest_path
    else:
        session.add(FolderMap(
            user_email=user_email,
            workload=workload,
            source_path=source_path,
            dest_id=dest_id,
            dest_path=dest_path,
        ))


# ── sync_cursor helpers ───────────────────────────────────────────────────────

def get_cursor(session: Session, user_email: str, workload: str) -> str | None:
    return session.execute(
        select(SyncCursor.cursor_value)
        .where(
            SyncCursor.user_email == user_email,
            SyncCursor.workload == workload,
        )
    ).scalar_one_or_none()


def save_cursor(session: Session, user_email: str, workload: str, cursor_value: str) -> None:
    stmt = sqlite_insert(SyncCursor).values(
        user_email=user_email,
        workload=workload,
        cursor_value=cursor_value,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_email", "workload"],
        set_={"cursor_value": stmt.excluded.cursor_value, "updated_at": func.now()},
    )
    session.execute(stmt)
