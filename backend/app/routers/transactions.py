from typing import Optional
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.transaction import (
    TransactionType,
    TransactionResponse,
    CreditTransactionCreate,
    PaymentTransactionCreate,
    TransactionCreate,
)
from app.services import ledger_service

router = APIRouter(prefix="/transactions", tags=["Transactions"])


class DirectCreditRequest(BaseModel):
    customer_id: int = Field(..., gt=0, description="Customer ID")
    amount: float = Field(..., gt=0, description="Transaction amount (must be > 0)")
    description: Optional[str] = Field(None, max_length=255)


class DirectPaymentRequest(BaseModel):
    customer_id: int = Field(..., gt=0, description="Customer ID")
    amount: float = Field(..., gt=0, description="Transaction amount (must be > 0)")
    description: Optional[str] = Field(None, max_length=255)


class DirectTransactionRequest(BaseModel):
    customer_id: int = Field(..., gt=0, description="Customer ID")
    type: TransactionType
    amount: float = Field(..., gt=0, description="Transaction amount (must be > 0)")
    description: Optional[str] = Field(None, max_length=255)


@router.post(
    "/credit",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Directly record a credit / udhaar transaction",
)
def record_credit(
    payload: DirectCreditRequest,
    db: Session = Depends(get_db),
):
    return ledger_service.add_credit_transaction(
        db,
        payload.customer_id,
        CreditTransactionCreate(
            amount=payload.amount,
            description=payload.description,
        ),
    )


@router.post(
    "/payment",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Directly record a payment transaction",
)
def record_payment(
    payload: DirectPaymentRequest,
    db: Session = Depends(get_db),
):
    return ledger_service.add_payment_transaction(
        db,
        payload.customer_id,
        PaymentTransactionCreate(
            amount=payload.amount,
            description=payload.description,
        ),
    )


@router.post(
    "",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Directly record a generic transaction",
)
def record_transaction(
    payload: DirectTransactionRequest,
    db: Session = Depends(get_db),
):
    return ledger_service.add_transaction(
        db,
        payload.customer_id,
        TransactionCreate(
            type=payload.type,
            amount=payload.amount,
            description=payload.description,
        ),
    )
