"""Offline migration from a MaiBot deployment into the Life layer (S7b).

The importer reads a MaiBot instance strictly read-only and writes only Life
tables, so the old deployment stays untouched and a rollback has to clean one
side only. Every dataset is idempotent and a dry run reports what would be
written without touching the database.
"""

from __future__ import annotations

MIGRATION_DATASETS = (
    "messages",
    "people",
    "expressions",
    "jargon",
    "emoji",
    "memory",
)
"""Datasets in the order they are imported."""
