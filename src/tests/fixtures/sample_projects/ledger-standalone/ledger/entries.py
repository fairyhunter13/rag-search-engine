from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

EntryType = Literal["debit", "credit"]


@dataclass
class JournalEntry:
    entry_id: str
    account_id: str
    entry_type: EntryType
    amount: Decimal
    description: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    reference: str = ""


def make_debit(account_id: str, amount: Decimal, description: str, ref: str = "") -> JournalEntry:
    if amount <= 0:
        raise ValueError(f"Debit amount must be positive, got {amount}")
    return JournalEntry(
        entry_id=f"dbt-{account_id}-{amount}",
        account_id=account_id,
        entry_type="debit",
        amount=amount,
        description=description,
        reference=ref,
    )


def make_credit(account_id: str, amount: Decimal, description: str, ref: str = "") -> JournalEntry:
    if amount <= 0:
        raise ValueError(f"Credit amount must be positive, got {amount}")
    return JournalEntry(
        entry_id=f"crd-{account_id}-{amount}",
        account_id=account_id,
        entry_type="credit",
        amount=amount,
        description=description,
        reference=ref,
    )


def entry_balance(entry: JournalEntry) -> Decimal:
    if entry.entry_type == "debit":
        return entry.amount
    return -entry.amount


def validate_entry(entry: JournalEntry) -> None:
    if not entry.entry_id:
        raise ValueError("entry_id is required")
    if not entry.account_id:
        raise ValueError("account_id is required")
    if entry.amount <= 0:
        raise ValueError(f"amount must be positive, got {entry.amount}")
    if entry.entry_type not in ("debit", "credit"):
        raise ValueError(f"entry_type must be 'debit' or 'credit', got {entry.entry_type!r}")
