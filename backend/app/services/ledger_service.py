from sqlalchemy import func
from datetime import datetime, timedelta, timezone
from typing import List
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Transaction, TransactionType
from app.schemas.transaction import (
    TransactionCreate,
    CreditTransactionCreate,
    PaymentTransactionCreate,
    TransactionResponse,
)
from app.services.customer_service import get_customer_by_id


def add_transaction(
    db: Session,
    customer_id: int,
    tx_in: TransactionCreate,
    date_offset: int = 0,
) -> TransactionResponse:
    # Validate customer exists (raises 404/400 if invalid)
    get_customer_by_id(db, customer_id)

    if tx_in.amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Transaction amount must be greater than zero",
        )

    transaction_date = datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(days=date_offset)

    tx = Transaction(
        customer_id=customer_id,
        type=tx_in.type.value,
        amount=tx_in.amount,
        description=tx_in.description,
        created_at=transaction_date,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    return TransactionResponse(
        id=tx.id,
        customer_id=tx.customer_id,
        type=TransactionType(tx.type),
        amount=tx.amount,
        description=tx.description,
        created_at=tx.created_at,
    )


def add_credit_transaction(
    db: Session,
    customer_id: int,
    credit_in: CreditTransactionCreate,
    date_offset: int = 0,
) -> TransactionResponse:
    tx_in = TransactionCreate(
        type=TransactionType.CREDIT,
        amount=credit_in.amount,
        description=credit_in.description,
    )
    return add_transaction(db, customer_id, tx_in, date_offset=date_offset)


def add_payment_transaction(
    db: Session,
    customer_id: int,
    payment_in: PaymentTransactionCreate,
    date_offset: int = 0,
) -> TransactionResponse:
    tx_in = TransactionCreate(
        type=TransactionType.PAYMENT,
        amount=payment_in.amount,
        description=payment_in.description,
    )
    return add_transaction(db, customer_id, tx_in, date_offset=date_offset)


def update_transaction_amount(
    db: Session,
    transaction_id: int,
    amount: float,
) -> TransactionResponse:
    """Update only the amount of an existing transaction."""
    if amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Transaction amount must be greater than zero",
        )

    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction with ID {transaction_id} not found",
        )

    tx.amount = amount
    db.commit()
    db.refresh(tx)

    return TransactionResponse(
        id=tx.id,
        customer_id=tx.customer_id,
        type=TransactionType(tx.type),
        amount=tx.amount,
        description=tx.description,
        created_at=tx.created_at,
    )


def get_daily_total(
    db: Session,
    transaction_type: TransactionType,
    date_offset: int = 0,
) -> float:
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    start = (now + timedelta(days=date_offset)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(days=1)

    total = (
        db.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(
            Transaction.type == transaction_type.value,
            Transaction.created_at >= start,
            Transaction.created_at < end,
        )
        .scalar()
    )
    return float(total or 0.0)

def get_customer_transactions(
    db: Session,
    customer_id: int,
) -> List[TransactionResponse]:
    # Validate customer exists (raises 404/400 if invalid)
    get_customer_by_id(db, customer_id)

    transactions = (
        db.query(Transaction)
        .filter(Transaction.customer_id == customer_id)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .all()
    )

    return [
        TransactionResponse(
            id=t.id,
            customer_id=t.customer_id,
            type=TransactionType(t.type),
            amount=t.amount,
            description=t.description,
            created_at=t.created_at,
        )
        for t in transactions
    ]
    # Validate customer exists (raises 404/400 if invalid)
    get_customer_by_id(db, customer_id)

    transactions = (
        db.query(Transaction)
        .filter(Transaction.customer_id == customer_id)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .all()
    )

    return [
        TransactionResponse(
            id=t.id,
            customer_id=t.customer_id,
            type=TransactionType(t.type),
            amount=t.amount,
            description=t.description,
            created_at=t.created_at,
        )
        for t in transactions
    ]
