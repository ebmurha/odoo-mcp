"""Strict public inputs for controlled draft Payroll writes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

PositiveIdentifier = Annotated[StrictInt, Field(gt=0)]


class PayrollWriteInput(BaseModel):
    """Controls shared by the three draft-only Payroll write tools."""

    model_config = ConfigDict(extra="forbid")

    company_id: PositiveIdentifier
    payslip_id: PositiveIdentifier
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class SetDraftPayrollInput(PayrollWriteInput):
    """Create or update one exact one-off input on an editable payslip."""

    input_id: PositiveIdentifier | None = None
    input_type_id: PositiveIdentifier | None = None
    description: str | None = None
    amount: Decimal | None = None

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 200:
            raise ValueError("description must contain 1 to 200 characters")
        return normalized

    @field_validator("amount", mode="before")
    @classmethod
    def parse_decimal_string(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("amount must be a decimal string")
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise ValueError("amount must be a decimal string") from None
        exponent = amount.as_tuple().exponent
        if (
            not amount.is_finite()
            or abs(amount) > Decimal("1000000000000")
            or not isinstance(exponent, int)
            or exponent < -6
        ):
            raise ValueError("amount is outside the supported precision or bound")
        return amount

    @model_validator(mode="after")
    def validate_operation(self) -> SetDraftPayrollInput:
        if self.input_id is None:
            if self.description is None or self.amount is None:
                raise ValueError("create requires description and amount")
            if not self.dry_run and self.input_type_id is None:
                raise ValueError("create execution requires input_type_id")
        else:
            if self.input_type_id is not None:
                raise ValueError("input_type_id is immutable during update")
            if self.description is None and self.amount is None:
                raise ValueError("update requires description or amount")
        return self


class RemoveDraftPayrollInput(PayrollWriteInput):
    """Remove one exact eligible input from an editable payslip."""

    input_id: PositiveIdentifier


class RecalculateDraftPayslip(PayrollWriteInput):
    """Invoke Odoo's standard calculation on one exact editable payslip."""
