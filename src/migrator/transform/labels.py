from __future__ import annotations

from typing import Literal

# System Gmail labels → Outlook well-known folder names
SYSTEM_LABEL_FOLDER = {
    "INBOX": "Inbox",
    "SENT": "SentItems",
    "DRAFT": "Drafts",
    "TRASH": "DeletedItems",
    "SPAM": "JunkEmail",
    "STARRED": None,    # → SourceMessage.is_flagged (set by the connector)
    "IMPORTANT": None,  # → "Important" category (set by the connector)
    "CHAT": None,       # skip — Chat is out of scope
    "UNREAD": None,     # → SourceMessage.is_read (set by the connector)
}

MultiLabelPolicy = Literal["categories", "duplicate"]


def label_to_folder_path(label_name: str) -> str:
    """Convert a Gmail label name (with / separators) to an Outlook folder path."""
    return label_name.replace("/", "\\")


def resolve_label_placement(
    label_ids: list[str],
    label_map: dict[str, str],  # label_id → label_name
    policy: MultiLabelPolicy,
) -> tuple[list[str], list[str]]:
    """Return (folder_paths, categories) for a message given its labels.

    - System labels are resolved to well-known folder names (or None → skip).
    - User labels become folder paths (policy=duplicate) or the first becomes the
      folder and the rest become categories (policy=categories).
    - 'All Mail' label is always skipped to avoid duplicates.
    """
    user_labels = []
    system_folders = []
    categories = []

    for lid in label_ids:
        name = label_map.get(lid, lid)
        if name == "CATEGORY_PERSONAL" or name.startswith("CATEGORY_"):
            continue
        folder = SYSTEM_LABEL_FOLDER.get(name)
        if name in SYSTEM_LABEL_FOLDER:
            if folder:
                system_folders.append(folder)
        else:
            user_labels.append(label_to_folder_path(name))

    if not user_labels:
        return system_folders or ["Inbox"], []

    if policy == "duplicate":
        return (system_folders or []) + user_labels, []
    else:  # categories
        primary = user_labels[0]
        categories = user_labels[1:]
        return (system_folders or []) + [primary], categories
