from __future__ import annotations

from decimal import Decimal

import pytest
from ledger.accounts import Account, ChartOfAccounts
from ledger.entries import entry_balance, make_credit, make_debit, validate_entry
from ledger.journal import Journal, Transaction, double_entry


def _chart() -> ChartOfAccounts:
    chart = ChartOfAccounts()
    chart.add(Account("cash", "Cash", "asset"))
    chart.add(Account("ar", "Accounts Receivable", "asset"))
    chart.add(Account("revenue", "Revenue", "revenue"))
    chart.add(Account("expense", "Operating Expense", "expense"))
    chart.add(Account("equity", "Owner Equity", "equity"))
    return chart


def test_make_debit():
    e = make_debit("cash", Decimal("100"), "test")
    assert e.entry_type == "debit"
    assert e.amount == Decimal("100")


def test_make_credit():
    e = make_credit("revenue", Decimal("100"), "test")
    assert e.entry_type == "credit"
    assert e.amount == Decimal("100")


def test_entry_balance():
    assert entry_balance(make_debit("a", Decimal("50"), "x")) == Decimal("50")
    assert entry_balance(make_credit("a", Decimal("50"), "x")) == Decimal("-50")


def test_debit_nonpositive_raises():
    with pytest.raises(ValueError):
        make_debit("a", Decimal("0"), "x")


def test_validate_entry_missing_id():
    e = make_debit("cash", Decimal("10"), "x")
    e.entry_id = ""
    with pytest.raises(ValueError, match="entry_id"):
        validate_entry(e)


def test_chart_add_and_get():
    chart = ChartOfAccounts()
    acc = Account("cash", "Cash", "asset")
    chart.add(acc)
    assert chart.get("cash") is acc
    assert chart.size() == 1


def test_chart_duplicate_raises():
    chart = ChartOfAccounts()
    chart.add(Account("cash", "Cash", "asset"))
    with pytest.raises(ValueError, match="already exists"):
        chart.add(Account("cash", "Cash", "asset"))


def test_chart_invalid_type_raises():
    chart = ChartOfAccounts()
    with pytest.raises(ValueError, match="Invalid account type"):
        chart.add(Account("x", "X", "unknown_type"))


def test_chart_list_by_type():
    chart = _chart()
    assets = chart.list_by_type("asset")
    assert len(assets) == 2


def test_double_entry_balanced():
    chart = _chart()
    j = Journal(chart)
    double_entry(j, "t1", "Sale", "cash", "revenue", Decimal("200"))
    assert j.account_balance("cash") == Decimal("200")
    assert j.account_balance("revenue") == Decimal("200")


def test_trial_balance():
    chart = _chart()
    j = Journal(chart)
    double_entry(j, "t1", "Sale", "cash", "revenue", Decimal("500"))
    tb = j.trial_balance()
    assert tb["cash"] == Decimal("500")
    assert tb["revenue"] == Decimal("500")
    assert tb["ar"] == Decimal("0")


def test_unbalanced_transaction_raises():
    chart = _chart()
    j = Journal(chart)
    txn = Transaction("t_bad", "bad")
    txn.add_entry(make_debit("cash", Decimal("100"), "x"))
    with pytest.raises(ValueError, match="not balanced"):
        j.post(txn)


def test_unknown_account_raises():
    chart = _chart()
    j = Journal(chart)
    txn = Transaction("t_unk", "unk")
    txn.add_entry(make_debit("ghost", Decimal("50"), "x"))
    txn.add_entry(make_credit("cash", Decimal("50"), "x"))
    with pytest.raises(ValueError, match="not in chart"):
        j.post(txn)


def test_reconcile():
    chart = _chart()
    j = Journal(chart)
    double_entry(j, "t1", "Sale", "cash", "revenue", Decimal("300"))
    assert j.reconcile("cash", Decimal("300"))
    assert not j.reconcile("cash", Decimal("100"))


def test_transaction_count():
    chart = _chart()
    j = Journal(chart)
    double_entry(j, "t1", "Sale A", "cash", "revenue", Decimal("100"))
    double_entry(j, "t2", "Sale B", "cash", "revenue", Decimal("200"))
    assert j.transaction_count() == 2
