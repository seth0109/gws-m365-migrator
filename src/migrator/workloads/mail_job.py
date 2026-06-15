from __future__ import annotations

import email as email_lib
import logging
from typing import Any

from ..config import UserMapping
from ..google.gmail import (
    decode_raw_mime,
    get_history_id,
    iter_message_metadata,
    iter_messages,
    list_labels,
)
from ..microsoft.graph_client import GraphClient
from ..microsoft.mail import ensure_mail_folder, import_mime_message, patch_message_flags
from ..state.db import get_cursor, is_done, save_cursor, session_scope, upsert_folder, upsert_item
from ..transform.labels import MultiLabelPolicy, resolve_label_placement

log = logging.getLogger(__name__)


def run_mail(user: UserMapping, gc: GraphClient | None, mode: str) -> None:
    import migrator as _pkg
    cfg = _pkg._current_config
    assert cfg is not None

    google_cfg = cfg.google
    policy: MultiLabelPolicy = cfg.workloads.mail.multi_label_policy

    if mode == "whatif":
        _whatif_mail(user, google_cfg, policy)
        return

    assert gc is not None, "GraphClient required outside whatif mode"

    ms_user = gc.get(f"/users/{user.ms_upn}", params={"$select": "id"})
    ms_user_id: str = ms_user["id"]

    # Capture history cursor before we start reading (for delta pass use)
    if mode == "full":
        history_id = get_history_id(google_cfg, user.google_email)
        with session_scope() as s:
            save_cursor(s, user.google_email, "mail", history_id)
        history_id_for_read = None
    else:
        with session_scope() as s:
            history_id_for_read = get_cursor(s, user.google_email, "mail")

    # Build label map and folder cache
    raw_labels = list_labels(google_cfg, user.google_email)
    label_map: dict[str, str] = {lbl["id"]: lbl["name"] for lbl in raw_labels}
    folder_cache: dict[str, str] = {}  # folder_path → graph_folder_id

    def _get_folder(path: str) -> str:
        if path in folder_cache:
            return folder_cache[path]
        parts = path.split("\\")
        parent_id: str | None = None
        for part in parts:
            current_path = "\\".join(parts[: parts.index(part) + 1])
            if current_path in folder_cache:
                parent_id = folder_cache[current_path]
            else:
                fid = ensure_mail_folder(gc, ms_user_id, part, parent_id)
                folder_cache[current_path] = fid
                with session_scope() as s:
                    upsert_folder(s, user.google_email, "mail", current_path, fid, current_path)
                parent_id = fid
        return folder_cache[path]

    for msg in iter_messages(google_cfg, user.google_email, history_id=history_id_for_read):
        msg_id: str = msg.get("id", "")

        # Use Message-ID header as dedup hash
        raw_bytes = decode_raw_mime(msg.get("raw", ""))
        parsed = email_lib.message_from_bytes(raw_bytes)
        source_hash = parsed.get("Message-ID", msg_id)

        with session_scope() as s:
            if is_done(s, user.google_email, "mail", msg_id):
                continue

        label_ids: list[str] = msg.get("labelIds", [])
        is_unread = "UNREAD" in label_ids
        is_read = not is_unread

        folder_paths, categories = resolve_label_placement(label_ids, label_map, policy)

        try:
            for folder_path in folder_paths:
                folder_id = _get_folder(folder_path)
                dest_id = import_mime_message(gc, ms_user_id, folder_id, raw_bytes)
                patch_message_flags(gc, ms_user_id, dest_id, is_read=is_read, categories=categories or None)

            with session_scope() as s:
                upsert_item(
                    s, user.google_email, "mail", msg_id,
                    source_hash=source_hash, dest_id=dest_id, status="done",
                )
        except Exception as exc:
            log.error("Failed message %s: %s", msg_id, exc)
            with session_scope() as s:
                upsert_item(s, user.google_email, "mail", msg_id, status="failed", last_error=str(exc))


def _whatif_mail(user: UserMapping, google_cfg: Any, policy: MultiLabelPolicy) -> None:
    import migrator as _pkg
    manifest = _pkg._current_manifest
    assert manifest is not None, "ManifestWriter must be set in whatif mode"

    raw_labels = list_labels(google_cfg, user.google_email)
    label_map: dict[str, str] = {lbl["id"]: lbl["name"] for lbl in raw_labels}

    for msg in iter_message_metadata(google_cfg, user.google_email):
        msg_id = msg.get("id", "")
        if not msg_id:
            continue
        label_ids = msg.get("labelIds", [])
        folder_paths, categories = resolve_label_placement(label_ids, label_map, policy)
        primary_folder = folder_paths[0] if folder_paths else "Inbox"
        extra = f"+{len(folder_paths) - 1} more" if len(folder_paths) > 1 else ""
        notes_parts = []
        if extra:
            notes_parts.append(f"folders: {primary_folder}{extra}")
        if categories:
            notes_parts.append(f"categories: {','.join(categories)}")
        if "UNREAD" in label_ids:
            notes_parts.append("unread")

        headers = msg.get("headers", {})
        manifest.add(
            user_email=user.google_email,
            ms_upn=user.ms_upn,
            workload="mail",
            source_id=msg_id,
            source_path=primary_folder,
            name=headers.get("Subject", "(no subject)"),
            size_bytes=msg.get("sizeEstimate", ""),
            modified_time=headers.get("Date", ""),
            notes="; ".join(notes_parts) or f"from={headers.get('From', '')}",
        )
