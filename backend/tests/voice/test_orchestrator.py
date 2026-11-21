import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.models import Customer, Transaction
from app.voice import orchestrator
from app.voice.command_parser import Intent, ParsedCommand
from app.voice.orchestrator import (
    STATUS_CUSTOMER_EXISTS,
    STATUS_CUSTOMER_NOT_FOUND,
    STATUS_ERROR,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_NEEDS_CUSTOMER_CLARIFICATION,
    STATUS_NEEDS_CUSTOMER_CONFIRMATION,
    STATUS_NEEDS_REPEAT,
    STATUS_SUCCESS,
    STATUS_UNKNOWN,
    process_voice_command,
    process_voice_commands,
)


def make_customer(db, name, phone=None):
    customer = Customer(name=name, phone=phone)
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def add_transaction(db, customer_id, tx_type, amount, description=None):
    tx = Transaction(
        customer_id=customer_id,
        type=tx_type,
        amount=amount,
        description=description,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return tx


def transaction_count(db):
    return db.query(Transaction).count()


def exact_name_count(db, name):
    return db.query(Customer).filter(Customer.name == name).count()


def test_credit_single_customer_records_transaction(db_session):
    customer = make_customer(db_session, "Ramesh")

    result = process_voice_command(db_session, "Ramesh ko 500 rupaye udhaar")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "CREDIT"
    assert result.customer["id"] == customer.id
    assert result.transaction["type"] == "CREDIT"
    assert result.transaction["amount"] == 500.0

    stored = db_session.query(Transaction).all()
    assert len(stored) == 1
    assert stored[0].customer_id == customer.id
    assert stored[0].type == "CREDIT"
    assert stored[0].amount == 500.0


def test_payment_single_customer_records_transaction(db_session):
    customer = make_customer(db_session, "Ramesh")

    result = process_voice_command(db_session, "Ramesh ne 200 rupaye jama kiye")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "PAYMENT"
    assert result.transaction["type"] == "PAYMENT"
    assert result.transaction["amount"] == 200.0

    stored = db_session.query(Transaction).all()
    assert len(stored) == 1
    assert stored[0].customer_id == customer.id
    assert stored[0].type == "PAYMENT"


def test_unknown_customer_asks_confirmation_and_creates_nothing(db_session):
    customers_before = db_session.query(Customer).count()

    result = process_voice_command(db_session, "Unknown ko 500 udhaar")

    assert result.status == STATUS_NEEDS_CUSTOMER_CONFIRMATION
    assert result.intent == "CREDIT"
    assert "add" in result.response_text.lower() or "naya" in result.response_text.lower()
    assert transaction_count(db_session) == 0
    assert db_session.query(Customer).count() == customers_before


def test_multiple_matches_ask_for_clarification(db_session):
    make_customer(db_session, "Ramesh")
    make_customer(db_session, "Ramesh")

    result = process_voice_command(db_session, "Ramesh ko 100 udhaar")

    assert result.status == STATUS_NEEDS_CUSTOMER_CLARIFICATION
    assert result.intent == "CREDIT"
    assert "Ramesh" in result.response_text
    assert transaction_count(db_session) == 0


def test_missing_customer_name_asks_for_name(db_session):
    result = process_voice_command(db_session, "500 rupaye udhaar")

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.intent == "CLARIFICATION_NEEDED"
    assert result.response_text
    assert transaction_count(db_session) == 0


def test_missing_amount_asks_for_amount(db_session):
    make_customer(db_session, "Ramesh")

    result = process_voice_command(db_session, "Ramesh ko udhaar")

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.response_text
    assert transaction_count(db_session) == 0


def test_zero_amount_asks_for_amount(db_session):
    make_customer(db_session, "Ramesh")

    result = process_voice_command(db_session, "Ramesh ko 0 rupaye udhaar")

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert transaction_count(db_session) == 0


@pytest.mark.parametrize("amount", [0.0, -50.0])
def test_non_positive_amount_guard(db_session, monkeypatch, amount):
    make_customer(db_session, "Ramesh")
    crafted = ParsedCommand(
        intent=Intent.CREDIT,
        customer_name="Ramesh",
        amount=amount,
    )
    monkeypatch.setattr(orchestrator, "parse_command", lambda transcript: crafted)

    result = process_voice_command(db_session, "Ramesh ko amount udhaar")

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert transaction_count(db_session) == 0


def test_balance_query_reports_totals(db_session):
    customer = make_customer(db_session, "Ramesh")
    add_transaction(db_session, customer.id, "CREDIT", 500.0)
    add_transaction(db_session, customer.id, "PAYMENT", 200.0)

    result = process_voice_command(db_session, "Ramesh ka kitna baaki hai")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "BALANCE_QUERY"
    assert result.customer["id"] == customer.id
    assert "300.00" in result.response_text
    assert "500.00" in result.response_text
    assert "200.00" in result.response_text
    assert transaction_count(db_session) == 2


def test_balance_query_unknown_customer(db_session):
    result = process_voice_command(db_session, "Suresh ka kitna baaki hai")

    assert result.status == STATUS_CUSTOMER_NOT_FOUND
    assert result.intent == "BALANCE_QUERY"


def test_history_query_with_transactions(db_session):
    customer = make_customer(db_session, "Ramesh")
    add_transaction(db_session, customer.id, "CREDIT", 500.0)
    add_transaction(db_session, customer.id, "PAYMENT", 200.0)

    result = process_voice_command(db_session, "Ramesh ki history")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "HISTORY_QUERY"
    assert result.customer["id"] == customer.id
    assert result.transactions is not None
    assert len(result.transactions) == 2
    assert {tx["type"] for tx in result.transactions} == {"CREDIT", "PAYMENT"}


def test_history_query_without_transactions(db_session):
    customer = make_customer(db_session, "Suresh")

    result = process_voice_command(db_session, "Suresh ki history")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "HISTORY_QUERY"
    assert result.customer["id"] == customer.id
    assert result.transactions == []
    assert result.response_text


def test_new_customer_creates_when_name_present(db_session):
    result = process_voice_command(db_session, "Ramesh ka naya khata kholo")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "NEW_CUSTOMER"
    assert result.customer["name"] == "Ramesh"
    assert exact_name_count(db_session, "Ramesh") == 1


def test_new_customer_without_name_asks_for_name(db_session):
    result = process_voice_command(db_session, "naya customer add karo")

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.intent == "NEW_CUSTOMER"
    assert db_session.query(Customer).count() == 0


def test_existing_customer_is_not_duplicated(db_session):
    first = process_voice_command(db_session, "Ramesh ka naya khata kholo")
    assert first.status == STATUS_SUCCESS

    second = process_voice_command(db_session, "Ramesh ka naya khata kholo")

    assert second.status == STATUS_CUSTOMER_EXISTS
    assert exact_name_count(db_session, "Ramesh") == 1


def test_correction_does_not_create_transaction(db_session):
    make_customer(db_session, "Ramesh")

    result = process_voice_command(db_session, "nahi, 430")

    assert result.status == STATUS_NEEDS_REPEAT
    assert result.intent == "CORRECTION"
    assert result.response_text
    assert transaction_count(db_session) == 0


def test_spoken_correction_updates_immediately_preceding_transaction(db_session):
    customer = make_customer(db_session, "Rahul")

    first = process_voice_command(db_session, "Rahul ko 500 rupaye udhaar")
    assert first.status == STATUS_SUCCESS
    assert first.transaction is not None

    corrected = process_voice_command(
        db_session,
        "Nahi, 430",
        pending_transaction_id=first.transaction["id"],
    )

    assert corrected.status == STATUS_SUCCESS
    assert corrected.intent == "CORRECTION"
    assert corrected.transaction["id"] == first.transaction["id"]
    assert corrected.transaction["amount"] == 430.0
    assert "Theek hai, Rahul ka CREDIT amount 500 se 430 rupaye kar diya." in corrected.response_text

    stored = db_session.query(Transaction).all()
    assert len(stored) == 1
    assert stored[0].id == first.transaction["id"]
    assert stored[0].customer_id == customer.id
    assert stored[0].type == "CREDIT"
    assert stored[0].amount == 430.0


def test_spoken_correction_without_pending_transaction_asks_repeat(db_session):
    make_customer(db_session, "Rahul")

    result = process_voice_command(db_session, "Nahi, 430")

    assert result.status == STATUS_NEEDS_REPEAT
    assert result.intent == "CORRECTION"
    assert transaction_count(db_session) == 0


def test_spoken_correction_with_missing_amount_asks_clarification(db_session, monkeypatch):
    customer = make_customer(db_session, "Rahul")
    first = process_voice_command(db_session, "Rahul ko 500 rupaye udhaar")
    assert first.status == STATUS_SUCCESS

    crafted = ParsedCommand(
        intent=Intent.CORRECTION,
        amount=None,
        is_correction=True,
    )
    monkeypatch.setattr(orchestrator, "parse_command", lambda transcript: crafted)

    result = process_voice_command(
        db_session,
        "Nahi",
        pending_transaction_id=first.transaction["id"],
    )

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.intent == "CORRECTION"

    stored = db_session.query(Transaction).filter(Transaction.id == first.transaction["id"]).one()
    assert stored.amount == 500.0
    assert customer.id == stored.customer_id


def test_spoken_correction_is_session_scoped(db_session):
    first_customer = make_customer(db_session, "Rahul")
    second_customer = make_customer(db_session, "Amit")

    first = process_voice_command(db_session, "Rahul ko 500 rupaye udhaar")
    second = process_voice_command(db_session, "Amit ko 700 rupaye udhaar")
    assert first.status == STATUS_SUCCESS
    assert second.status == STATUS_SUCCESS

    corrected = process_voice_command(
        db_session,
        "Nahi, 430",
        pending_transaction_id=first.transaction["id"],
    )

    assert corrected.status == STATUS_SUCCESS
    assert corrected.transaction["id"] == first.transaction["id"]
    assert corrected.transaction["amount"] == 430.0

    first_stored = db_session.query(Transaction).filter(Transaction.id == first.transaction["id"]).one()
    second_stored = db_session.query(Transaction).filter(Transaction.id == second.transaction["id"]).one()

    assert first_stored.amount == 430.0
    assert first_stored.customer_id == first_customer.id
    assert second_stored.amount == 700.0
    assert second_stored.customer_id == second_customer.id


def test_relative_date_today_stores_today(db_session):
    customer = make_customer(db_session, "Ramesh")

    result = process_voice_command(db_session, "Ramesh ko 500 rupaye udhaar")

    assert result.status == STATUS_SUCCESS
    stored = db_session.query(Transaction).one()
    assert stored.created_at.date() == datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


def test_relative_date_yesterday_stores_yesterday(db_session):
    customer = make_customer(db_session, "Ramesh")

    result = process_voice_command(db_session, "kal Ramesh ko 500 rupaye udhaar")

    assert result.status == STATUS_SUCCESS
    stored = db_session.query(Transaction).one()
    assert stored.created_at.date() == (datetime.now(timezone(timedelta(hours=5, minutes=30))) - timedelta(days=1)).date()


def test_relative_date_day_before_yesterday_stores_parso(db_session):
    customer = make_customer(db_session, "Ramesh")

    result = process_voice_command(db_session, "parso Ramesh ko 500 rupaye udhaar")

    assert result.status == STATUS_SUCCESS
    stored = db_session.query(Transaction).one()
    assert stored.created_at.date() == (datetime.now(timezone(timedelta(hours=5, minutes=30))) - timedelta(days=2)).date()


def test_multiple_credit_transactions_in_one_sentence(db_session):
    make_customer(db_session, "Rahul")
    make_customer(db_session, "Amit")

    result = process_voice_commands(db_session, "Rahul ko 500 rupaye udhaar aur Amit ko 300 rupaye udhaar")

    assert result.status == STATUS_SUCCESS
    assert len(result.transactions) == 2
    assert transaction_count(db_session) == 2


def test_mixed_credit_and_payment_in_one_sentence(db_session):
    make_customer(db_session, "Rahul")
    make_customer(db_session, "Amit")

    result = process_voice_commands(db_session, "Rahul ko 500 rupaye udhaar aur Amit ne 200 rupaye jama kiye")

    assert result.status == STATUS_SUCCESS
    assert len(result.transactions) == 2
    types = {tx["type"] for tx in result.transactions}
    assert types == {"CREDIT", "PAYMENT"}


def test_multiple_transactions_partial_failure_does_not_duplicate(db_session):
    make_customer(db_session, "Rahul")

    result = process_voice_commands(db_session, "Rahul ko 500 rupaye udhaar aur Unknown ko 200 rupaye udhaar")

    assert result.status == "partial_success"
    assert len(result.transactions) == 1
    assert transaction_count(db_session) == 1


def test_multiple_transactions_unknown_customer_creates_nothing_for_that_clause(db_session):
    make_customer(db_session, "Rahul")

    result = process_voice_commands(db_session, "Rahul ko 500 rupaye udhaar aur Unknown ko 200 rupaye udhaar")

    assert result.status == "partial_success"
    assert transaction_count(db_session) == 1


def test_multi_transaction_unknown_customer_regression(db_session):
    make_customer(db_session, "Rahul")

    result = process_voice_commands(db_session, "Unknown ko 100 rupaye udhaar aur Rahul ko 200 rupaye udhaar")

    assert result.status == "partial_success"
    assert len(result.transactions) == 1
    assert transaction_count(db_session) == 1


def test_combined_credit_and_correction_records_one_transaction_of_150(db_session):
    make_customer(db_session, "Rahul")

    result = process_voice_commands(
        db_session,
        "Rahul ko 100 rupaye udhaar die, are nahin, 150 rupaye",
    )

    assert result.status == "partial_success" or result.status == STATUS_SUCCESS
    assert transaction_count(db_session) == 1
    stored = db_session.query(Transaction).one()
    assert stored.amount == 150.0
    assert stored.customer.name == "Rahul"


def test_standalone_correction_updates_immediately_preceding_transaction(db_session):
    make_customer(db_session, "Rahul")

    first = process_voice_command(db_session, "Rahul ko 100 rupaye udhaar")
    assert first.status == STATUS_SUCCESS

    corrected = process_voice_command(db_session, "nahi, 150", pending_transaction_id=first.transaction["id"])
    assert corrected.status == STATUS_SUCCESS
    assert corrected.intent == "CORRECTION"

    stored = db_session.query(Transaction).one()
    assert stored.id == first.transaction["id"]
    assert stored.amount == 150.0


def test_unknown_customer_does_not_modify_other_customer_transaction(db_session):
    rahul = make_customer(db_session, "Rahul")
    add_transaction(db_session, rahul.id, "CREDIT", 100.0)

    result = process_voice_commands(db_session, "Unknown ko 50 rupaye udhaar aur Rahul ko 20 rupaye udhaar")

    assert result.status == "partial_success"
    assert transaction_count(db_session) == 2
    rahul_tx = db_session.query(Transaction).filter(Transaction.customer_id == rahul.id).all()
    assert len(rahul_tx) == 2


def test_unknown_intent_returns_helpful_response(db_session):
    result = process_voice_command(db_session, "hello kaise ho")

    assert result.status == STATUS_UNKNOWN
    assert result.intent == "UNKNOWN"
    assert len(result.response_text) > 0
    assert transaction_count(db_session) == 0


def test_clarification_cases_leave_database_unchanged(db_session):
    make_customer(db_session, "Ramesh")
    make_customer(db_session, "Ramesh")
    customers_before = db_session.query(Customer).count()
    transactions_before = transaction_count(db_session)

    responses = [
        process_voice_command(db_session, "Ramesh ko 100 udhaar"),
        process_voice_command(db_session, "500 rupaye udhaar"),
        process_voice_command(db_session, "Ramesh ko udhaar"),
        process_voice_command(db_session, "nahi, 430"),
        process_voice_command(db_session, "random unrelated words"),
    ]

    assert all(r.status != STATUS_SUCCESS for r in responses)
    assert all(r.status != STATUS_ERROR for r in responses)
    assert db_session.query(Customer).count() == customers_before
    assert transaction_count(db_session) == transactions_before

def test_devanagari_credit_matches_latin_customer(db_session):
    customer = make_customer(db_session, "Rahul Sharma")

    result = process_voice_command(db_session, "राहुल शर्मा को 500 रुपए उधार दिए।")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "CREDIT"
    assert result.customer["id"] == customer.id
    assert result.transaction["type"] == "CREDIT"
    assert result.transaction["amount"] == 500.0
    assert transaction_count(db_session) == 1


def test_devanagari_payment_matches_latin_customer(db_session):
    customer = make_customer(db_session, "Rahul Sharma")

    result = process_voice_command(db_session, "राहुल शर्मा ने 200 रुपए जमा किए।")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "PAYMENT"
    assert result.customer["id"] == customer.id
    assert result.transaction["type"] == "PAYMENT"
    assert result.transaction["amount"] == 200.0


def test_devanagari_balance_query(db_session):
    customer = make_customer(db_session, "Rahul Sharma")
    add_transaction(db_session, customer.id, "CREDIT", 500.0)

    result = process_voice_command(db_session, "राहुल शर्मा का बैलेंस कितना है?")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "BALANCE_QUERY"
    assert result.customer["id"] == customer.id


def test_devanagari_history_query(db_session):
    customer = make_customer(db_session, "Rahul Sharma")
    add_transaction(db_session, customer.id, "CREDIT", 500.0)

    result = process_voice_command(db_session, "राहुल शर्मा की हिस्ट्री दिखाओ।")

    assert result.status == STATUS_SUCCESS
    assert result.intent == "HISTORY_QUERY"
    assert result.customer["id"] == customer.id


def test_devanagari_unknown_customer_does_not_create_transaction(db_session):
    before = transaction_count(db_session)

    result = process_voice_command(db_session, "अज्ञात ग्राहक को 100 रुपए उधार दिए।")

    assert result.status == STATUS_NEEDS_CUSTOMER_CONFIRMATION
    assert transaction_count(db_session) == before


def test_devanagari_ambiguous_customer_does_not_create_transaction(db_session):
    make_customer(db_session, "Rahul Sharma")
    make_customer(db_session, "Rahul Sharma")

    before = transaction_count(db_session)

    result = process_voice_command(db_session, "राहुल शर्मा को 100 रुपए उधार दिए।")

    assert result.status == STATUS_NEEDS_CUSTOMER_CLARIFICATION
    assert transaction_count(db_session) == before


def test_combined_devanagari_credit_and_correction_records_150(db_session):
    make_customer(db_session, 'Rahul')

    result = process_voice_commands(
        db_session,
        'Rahul ko 100 rupaye udhaar die, are nahin, 150 rupaye',
    )

    assert result.status in {'success', 'partial_success'}
    assert transaction_count(db_session) == 1
    stored = db_session.query(Transaction).one()
    assert stored.amount == 150.0



def test_devanagari_history_request_returns_history_not_balance(db_session):
    customer = make_customer(db_session, 'Rahul')
    add_transaction(db_session, customer.id, 'CREDIT', 100.0)

    result = process_voice_command(db_session, '\u0930\u093e\u0939\u0941\u0932 \u0915\u0940 history \u092c\u0924\u093e\u0913')

    assert result.status == STATUS_SUCCESS
    assert result.intent == 'HISTORY_QUERY'
    assert result.transactions
    assert 'transaction mile' in result.response_text
    assert 'baaki balance' not in result.response_text



def test_daily_credit_total_aggregates_today(db_session):
    c1 = make_customer(db_session, 'Rahul')
    c2 = make_customer(db_session, 'Amit')
    add_transaction(db_session, c1.id, 'CREDIT', 100.0)
    add_transaction(db_session, c2.id, 'CREDIT', 250.0)
    add_transaction(db_session, c1.id, 'PAYMENT', 50.0)

    result = process_voice_command(db_session, 'Aaj kitna udhaar gaya?')

    assert result.status == STATUS_SUCCESS
    assert result.intent == 'DAILY_CREDIT_TOTAL'
    assert result.response_text == 'Aaj total 350 rupaye ka udhaar gaya.'
    assert transaction_count(db_session) == 3


def test_daily_payment_total_aggregates_today(db_session):
    c1 = make_customer(db_session, 'Rahul')
    add_transaction(db_session, c1.id, 'CREDIT', 100.0)
    add_transaction(db_session, c1.id, 'PAYMENT', 40.0)
    add_transaction(db_session, c1.id, 'PAYMENT', 60.0)

    result = process_voice_command(db_session, 'Aaj kitna jama hua?')

    assert result.status == STATUS_SUCCESS
    assert result.intent == 'DAILY_PAYMENT_TOTAL'
    assert result.response_text == 'Aaj total 100 rupaye jama hue.'
    assert transaction_count(db_session) == 3


def test_daily_total_zero_when_no_transactions(db_session):
    result = process_voice_command(db_session, 'Kal kitna udhaar gaya?')

    assert result.status == STATUS_SUCCESS
    assert result.intent == 'DAILY_CREDIT_TOTAL'
    assert result.response_text == 'Kal total 0 rupaye ka udhaar gaya.'

