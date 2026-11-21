from typing import List, Optional
from fastapi import APIRouter, Depends, Query, Path, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.customer import (
    CustomerCreate,
    CustomerResponse,
    CustomerBalanceResponse,
)
from app.schemas.transaction import (
    CreditTransactionCreate,
    PaymentTransactionCreate,
    TransactionCreate,
    TransactionResponse,
)
from app.services import customer_service, ledger_service

router = APIRouter(prefix="/customers", tags=["Customers"])


@router.post(
    "",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new customer",
)
def create_customer(
    customer_in: CustomerCreate,
    db: Session = Depends(get_db),
):
    return customer_service.create_customer(db, customer_in)


@router.get(
    "",
    response_model=List[CustomerResponse],
    summary="View all customers or search by name",
)
def list_customers(
    name: Optional[str] = Query(
        None, description="Search customers by name substring"
    ),
    search: Optional[str] = Query(
        None, description="Search customers by name substring (alias for name)"
    ),
    db: Session = Depends(get_db),
):
    search_term = name if name is not None else search
    return customer_service.get_customers(db, search=search_term)


@router.get(
    "/{customer_id}",
    response_model=CustomerResponse,
    summary="Get customer details and outstanding balance",
)
def get_customer(
    customer_id: int = Path(..., gt=0, description="Customer unique ID (must be > 0)"),
    db: Session = Depends(get_db),
):
    return customer_service.get_customer_details(db, customer_id)


@router.get(
    "/{customer_id}/balance",
    response_model=CustomerBalanceResponse,
    summary="Get customer balance breakdown",
)
def get_customer_balance(
    customer_id: int = Path(..., gt=0, description="Customer unique ID (must be > 0)"),
    db: Session = Depends(get_db),
):
    return customer_service.get_customer_balance(db, customer_id)


@router.get(
    "/{customer_id}/transactions",
    response_model=List[TransactionResponse],
    summary="Show transaction history for a customer",
)
def get_customer_transactions(
    customer_id: int = Path(..., gt=0, description="Customer unique ID (must be > 0)"),
    db: Session = Depends(get_db),
):
    return ledger_service.get_customer_transactions(db, customer_id)


@router.post(
    "/{customer_id}/credit",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a credit / udhaar transaction for a customer",
)
def add_credit_transaction(
    credit_in: CreditTransactionCreate,
    customer_id: int = Path(..., gt=0, description="Customer unique ID (must be > 0)"),
    db: Session = Depends(get_db),
):
    return ledger_service.add_credit_transaction(db, customer_id, credit_in)


@router.post(
    "/{customer_id}/payment",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a payment transaction for a customer",
)
def add_payment_transaction(
    payment_in: PaymentTransactionCreate,
    customer_id: int = Path(..., gt=0, description="Customer unique ID (must be > 0)"),
    db: Session = Depends(get_db),
):
    return ledger_service.add_payment_transaction(db, customer_id, payment_in)


@router.post(
    "/{customer_id}/transactions",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a transaction (credit or payment) for a customer",
)
def add_transaction(
    tx_in: TransactionCreate,
    customer_id: int = Path(..., gt=0, description="Customer unique ID (must be > 0)"),
    db: Session = Depends(get_db),
):
    return ledger_service.add_transaction(db, customer_id, tx_in)
