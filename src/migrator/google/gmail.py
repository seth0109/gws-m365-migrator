from __future__ import annotations

import base64
import logging
from typing import Any, Generator

from ..auth.google_auth import build_service
from ..config import GoogleConfig
from ..ratelimit import registry

log = logging.getLogger(__name__)


def _svc(cfg: GoogleConfig, email: str):
    return build_service("gmail", "v1", cfg.service_account_key_file, email)


def list_labels(cfg: GoogleConfig, user_email: str) -> list[dict[str, Any]]:
    svc = _svc(cfg, user_email)
    result = svc.users().labels().list(userId="me").execute()
    return result.get("labels", [])


def iter_messages(
    cfg: GoogleConfig,
    user_email: str,
    label_ids: list[str] | None = None,
    history_id: str | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Yield full raw MIME message dicts, one at a time.

    If *history_id* is set, yields only messages changed since that point (delta pass).
    """
    svc = _svc(cfg, user_email)

    if history_id:
        yield from _iter_history(svc, user_email, history_id)
        return

    params: dict[str, Any] = {"userId": "me", "maxResults": 500}
    if label_ids:
        params["labelIds"] = label_ids

    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        resp = svc.users().messages().list(**params).execute()
        messages = resp.get("messages", [])
        for stub in messages:
            try:
                registry.acquire("google_global")
            except KeyError:
                pass
            msg = svc.users().messages().get(userId="me", id=stub["id"], format="raw").execute()
            yield msg
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def _iter_history(svc: Any, user_email: str, start_history_id: str) -> Generator[dict, None, None]:
    params = {"userId": "me", "startHistoryId": start_history_id, "historyTypes": ["messageAdded"]}
    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        resp = svc.users().history().list(**params).execute()
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg_id = added["message"]["id"]
                try:
                    registry.acquire("google_global")
                except KeyError:
                    pass
                yield svc.users().messages().get(userId="me", id=msg_id, format="raw").execute()
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def iter_message_metadata(
    cfg: GoogleConfig,
    user_email: str,
) -> Generator[dict[str, Any], None, None]:
    """Yield lightweight message metadata (no raw MIME body) for inventory/whatif.

    Each yielded dict has: id, threadId, labelIds, snippet, sizeEstimate,
    internalDate, and a flattened headers dict for Subject/From/To/Date.
    """
    svc = _svc(cfg, user_email)
    params: dict[str, Any] = {"userId": "me", "maxResults": 500}
    page_token: str | None = None
    header_names = ["Subject", "From", "To", "Date", "Message-ID"]

    while True:
        if page_token:
            params["pageToken"] = page_token
        try:
            registry.acquire("google_global")
        except KeyError:
            pass
        resp = svc.users().messages().list(**params).execute()
        for stub in resp.get("messages", []):
            try:
                registry.acquire("google_global")
            except KeyError:
                pass
            msg = svc.users().messages().get(
                userId="me",
                id=stub["id"],
                format="metadata",
                metadataHeaders=header_names,
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            yield {
                "id": msg.get("id", ""),
                "threadId": msg.get("threadId", ""),
                "labelIds": msg.get("labelIds", []),
                "snippet": msg.get("snippet", ""),
                "sizeEstimate": msg.get("sizeEstimate", 0),
                "internalDate": msg.get("internalDate", ""),
                "headers": headers,
            }
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def get_history_id(cfg: GoogleConfig, user_email: str) -> str:
    svc = _svc(cfg, user_email)
    profile = svc.users().getProfile(userId="me").execute()
    return str(profile["historyId"])


def decode_raw_mime(raw_b64: str) -> bytes:
    return base64.urlsafe_b64decode(raw_b64 + "==")
