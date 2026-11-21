from typing import List, Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Customer, Transaction, TransactionType
from app.schemas.customer import (
    CustomerCreate,
    CustomerResponse,
    CustomerBalanceResponse,
)


def calculate_balance_for_customer(db: Session, customer_id: int) -> dict:
    """Calculate total credit, total payment, and current outstanding balance.
    
    Credit (Udhaar) increases what the customer owes (debt).
    Payment (Jama) decreases what the customer owes.
    Outstanding Balance = Total Credit - Total Payment.
    """
    transactions = (
        db.query(Transaction)
        .filter(Transaction.customer_id == customer_id)
        .all()
    )
    total_credit = sum(
        t.amount for t in transactions if t.type == TransactionType.CREDIT.value
    )
    total_payment = sum(
        t.amount for t in transactions if t.type == TransactionType.PAYMENT.value
    )
    current_balance = round(total_credit - total_payment, 2)
    return {
        "total_credit": round(total_credit, 2),
        "total_payment": round(total_payment, 2),
        "current_balance": current_balance,
    }


def create_customer(db: Session, customer_in: CustomerCreate) -> CustomerResponse:
    if customer_in.id is not None:
        if customer_in.id <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Customer ID must be greater than zero",
            )
        existing = db.query(Customer).filter(Customer.id == customer_in.id).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Customer with ID {customer_in.id} already exists",
            )
        customer = Customer(
            id=customer_in.id,
            name=customer_in.name,
            phone=customer_in.phone,
        )
    else:
        customer = Customer(
            name=customer_in.name,
            phone=customer_in.phone,
        )

    db.add(customer)
    db.commit()
    db.refresh(customer)

    return CustomerResponse(
        id=customer.id,
        name=customer.name,
        phone=customer.phone,
        current_balance=0.0,
        created_at=customer.created_at,
    )


def get_customer_by_id(db: Session, customer_id: int) -> Customer:
    if customer_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid customer ID. ID must be greater than zero",
        )
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer with ID {customer_id} not found",
        )
    return customer


def get_customer_details(db: Session, customer_id: int) -> CustomerResponse:
    customer = get_customer_by_id(db, customer_id)
    balances = calculate_balance_for_customer(db, customer.id)
    return CustomerResponse(
        id=customer.id,
        name=customer.name,
        phone=customer.phone,
        current_balance=balances["current_balance"],
        created_at=customer.created_at,
    )


def get_customers(
    db: Session, search: Optional[str] = None
) -> List[CustomerResponse]:
    query = db.query(Customer)
    if search and search.strip():
        query = query.filter(Customer.name.ilike(f"%{search.strip()}%"))

    customers = query.order_by(Customer.id.asc()).all()

    result: List[CustomerResponse] = []
    for c in customers:
        balances = calculate_balance_for_customer(db, c.id)
        result.append(
            CustomerResponse(
                id=c.id,
                name=c.name,
                phone=c.phone,
                current_balance=balances["current_balance"],
                created_at=c.created_at,
            )
        )
    return result


def get_customer_balance(
    db: Session, customer_id: int
) -> CustomerBalanceResponse:
    customer = get_customer_by_id(db, customer_id)
    balances = calculate_balance_for_customer(db, customer.id)
    return CustomerBalanceResponse(
        customer_id=customer.id,
        customer_name=customer.name,
        current_balance=balances["current_balance"],
        total_credit=balances["total_credit"],
        total_payment=balances["total_payment"],
    )
