from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, FolderMap, ItemMap, JobRun, SyncCursor

_engine = None
_SessionFactory = None


def init_db(db_path: Path) -> None:
    global _engine, _SessionFactory
    _engine = create_engine(f"sqlite:///{db_path}", echo=False)
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
        },
    )
    session.execute(stmt)


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
        set_={"cursor_value": stmt.excluded.cursor_value},
    )
    session.execute(stmt)
