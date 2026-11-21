from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransactionType(str, Enum):
    CREDIT = "CREDIT"
    PAYMENT = "PAYMENT"


class TransactionBase(BaseModel):
    amount: float = Field(..., gt=0, description="Transaction amount (must be > 0)")
    description: Optional[str] = Field(None, max_length=255, description="Optional transaction note")

    @field_validator("amount")
    @classmethod
    def validate_positive_amount(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Amount must be greater than zero")
        return round(float(v), 2)

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed if trimmed else None


class TransactionCreate(TransactionBase):
    type: TransactionType

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, v: Any) -> TransactionType:
        if isinstance(v, str):
            val = v.strip().upper()
            if val in ("CREDIT", "UDHAAR", "UDHAR", "DIYE"):
                return TransactionType.CREDIT
            if val in ("PAYMENT", "JAMA", "PAY", "MILE"):
                return TransactionType.PAYMENT
            try:
                return TransactionType(val)
            except ValueError:
                raise ValueError("Transaction type must be 'CREDIT' (udhaar) or 'PAYMENT' (jama)")
        return v


class CreditTransactionCreate(TransactionBase):
    """Payload for recording a credit/udhaar transaction."""
    pass


class PaymentTransactionCreate(TransactionBase):
    """Payload for recording a payment/jama transaction."""
    pass


class TransactionResponse(BaseModel):
    id: int
    customer_id: int
    type: TransactionType
    amount: float
    description: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
