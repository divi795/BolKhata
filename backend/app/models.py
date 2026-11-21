from datetime import datetime, timezone
import enum
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from app.database import Base


class TransactionType(str, enum.Enum):
    CREDIT = "CREDIT"
    PAYMENT = "PAYMENT"


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(100), nullable=False, index=True)
    phone = Column(String(20), nullable=True)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    transactions = relationship(
        "Transaction",
        back_populates="customer",
        cascade="all, delete-orphan",
        order_by="desc(Transaction.created_at)",
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    customer_id = Column(
        Integer,
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type = Column(String(20), nullable=False)  # CREDIT or PAYMENT
    amount = Column(Float, nullable=False)
    description = Column(String(255), nullable=True)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    customer = relationship("Customer", back_populates="transactions")
