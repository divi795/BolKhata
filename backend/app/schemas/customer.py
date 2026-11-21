from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CustomerBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Customer full name")
    phone: Optional[str] = Field(None, max_length=20, description="Optional phone number")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Customer name cannot be empty or whitespace only")
        return trimmed

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed if trimmed else None


class CustomerCreate(CustomerBase):
    id: Optional[int] = Field(None, gt=0, description="Optional custom unique customer ID")


class CustomerResponse(CustomerBase):
    id: int
    current_balance: float = 0.0
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CustomerBalanceResponse(BaseModel):
    customer_id: int
    customer_name: str
    current_balance: float
    total_credit: float
    total_payment: float
