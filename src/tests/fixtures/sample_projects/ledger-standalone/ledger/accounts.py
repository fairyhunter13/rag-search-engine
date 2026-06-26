from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

AccountType = str  # 'asset', 'liability', 'equity', 'revenue', 'expense'

DEBIT_NORMAL = frozenset({"asset", "expense"})
CREDIT_NORMAL = frozenset({"liability", "equity", "revenue"})


@dataclass
class Account:
    account_id: str
    name: str
    account_type: AccountType
    parent_id: Optional[str] = None
    tags: list[str] = field(default_factory=list)


def normal_balance_sign(account_type: AccountType) -> int:
    if account_type in DEBIT_NORMAL:
        return 1
    if account_type in CREDIT_NORMAL:
        return -1
    raise ValueError(f"Unknown account type: {account_type!r}")


def compute_account_balance(account: Account, entries: list) -> Decimal:
    from ledger.entries import entry_balance
    sign = normal_balance_sign(account.account_type)
    return Decimal(sum(sign * entry_balance(e) for e in entries if e.account_id == account.account_id))


class ChartOfAccounts:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}

    def add(self, account: Account) -> None:
        if account.account_id in self._accounts:
            raise ValueError(f"Account {account.account_id!r} already exists")
        if account.account_type not in (*DEBIT_NORMAL, *CREDIT_NORMAL):
            raise ValueError(f"Invalid account type: {account.account_type!r}")
        self._accounts[account.account_id] = account

    def get(self, account_id: str) -> Optional[Account]:
        return self._accounts.get(account_id)

    def remove(self, account_id: str) -> None:
        self._accounts.pop(account_id, None)

    def list_by_type(self, account_type: AccountType) -> list[Account]:
        return [a for a in self._accounts.values() if a.account_type == account_type]

    def all(self) -> list[Account]:
        return list(self._accounts.values())

    def size(self) -> int:
        return len(self._accounts)
