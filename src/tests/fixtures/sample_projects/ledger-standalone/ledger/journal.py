from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ledger.accounts import ChartOfAccounts, compute_account_balance
from ledger.entries import JournalEntry, make_credit, make_debit, validate_entry


@dataclass
class Transaction:
    transaction_id: str
    description: str
    entries: list[JournalEntry] = field(default_factory=list)

    def add_entry(self, entry: JournalEntry) -> None:
        validate_entry(entry)
        self.entries.append(entry)

    def is_balanced(self) -> bool:
        total = sum(
            e.amount if e.entry_type == "debit" else -e.amount
            for e in self.entries
        )
        return total == Decimal(0)

    def debit_total(self) -> Decimal:
        return Decimal(sum(e.amount for e in self.entries if e.entry_type == "debit"))

    def credit_total(self) -> Decimal:
        return Decimal(sum(e.amount for e in self.entries if e.entry_type == "credit"))

    def entry_count(self) -> int:
        return len(self.entries)


class Journal:
    def __init__(self, chart: ChartOfAccounts) -> None:
        self._chart = chart
        self._transactions: list[Transaction] = []

    def post(self, txn: Transaction) -> None:
        if not txn.entries:
            raise ValueError(f"Transaction {txn.transaction_id} has no entries")
        if not txn.is_balanced():
            raise ValueError(
                f"Transaction {txn.transaction_id} is not balanced: "
                f"debits={txn.debit_total()} credits={txn.credit_total()}"
            )
        for entry in txn.entries:
            if self._chart.get(entry.account_id) is None:
                raise ValueError(f"Account {entry.account_id!r} not in chart of accounts")
        self._transactions.append(txn)

    def entries_for_account(self, account_id: str) -> list[JournalEntry]:
        return [e for txn in self._transactions for e in txn.entries if e.account_id == account_id]

    def account_balance(self, account_id: str) -> Decimal:
        account = self._chart.get(account_id)
        if account is None:
            raise ValueError(f"Account {account_id!r} not found in chart")
        entries = self.entries_for_account(account_id)
        return compute_account_balance(account, entries)

    def trial_balance(self) -> dict[str, Decimal]:
        return {a.account_id: self.account_balance(a.account_id) for a in self._chart.all()}

    def reconcile(self, account_id: str, expected: Decimal) -> bool:
        return self.account_balance(account_id) == expected

    def transaction_count(self) -> int:
        return len(self._transactions)


def double_entry(
    journal: Journal,
    txn_id: str,
    description: str,
    debit_account: str,
    credit_account: str,
    amount: Decimal,
    ref: str = "",
) -> None:
    txn = Transaction(transaction_id=txn_id, description=description)
    txn.add_entry(make_debit(debit_account, amount, description, ref))
    txn.add_entry(make_credit(credit_account, amount, description, ref))
    journal.post(txn)
