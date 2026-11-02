"""Pydantic models for the server API and strict per-record validation."""

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import UUID4, AwareDatetime, BaseModel, Field, StringConstraints, ValidationError, field_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, strict=True)]
MeasurementValue = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Sequence = Annotated[int, Field(strict=True, ge=1)]


class Wbc(BaseModel):
    """White blood cell count."""

    value: MeasurementValue
    unit: Literal["10^3/uL"]


class Rbc(BaseModel):
    """Red blood cell count."""

    value: MeasurementValue
    unit: Literal["10^6/uL"]


class Hb(BaseModel):
    """Haemoglobin concentration."""

    value: MeasurementValue
    unit: Literal["g/dL"]


class Measurements(BaseModel):
    """The three values of one test, each with its exact unit."""

    wbc: Wbc
    rbc: Rbc
    hb: Hb


class RecordIn(BaseModel):
    """One record as sent by a device; received_at is never taken from the device."""

    event_id: UUID4
    device_id: NonEmptyStr
    sample_id: NonEmptyStr
    sequence: Sequence
    measured_at: AwareDatetime
    measurements: Measurements

    @field_validator("measured_at", mode="before")
    @classmethod
    def measured_at_is_text(cls, value: Any) -> Any:
        """Reject numbers and other non-string timestamps before datetime parsing."""
        if not isinstance(value, str):
            raise ValueError("must be an ISO-8601 string")
        return value

    @field_validator("measured_at")
    @classmethod
    def measured_at_is_utc(cls, value: datetime) -> datetime:
        """Accept only a zero UTC offset."""
        if value.utcoffset() != timedelta(0):
            raise ValueError("must be UTC (offset +00:00 or Z)")
        return value


class BatchRequest(BaseModel):
    """Batch envelope; items stay untyped so each record is validated on its own."""

    records: list[Any]


class RecordResult(BaseModel):
    """Outcome for one record in a batch."""

    event_id: str | None
    status: Literal["stored", "duplicate", "rejected"]
    error: str | None = None


class BatchResponse(BaseModel):
    """Per-record outcomes, in the same order as the request."""

    results: list[RecordResult]


class RecordOut(BaseModel):
    """A stored record including the server-assigned received_at."""

    event_id: str
    device_id: str
    sample_id: str
    sequence: int
    measured_at: str
    received_at: str
    measurements: dict[str, Any]


class RecordPage(BaseModel):
    """One page of a device's records ordered by sequence."""

    device_id: str
    records: list[RecordOut]


def format_errors(exc: ValidationError) -> str:
    """Flatten a ValidationError into 'field.path: message; ...'."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "record"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def validate_record(item: Any) -> tuple[str | None, RecordIn | str]:
    """Validate one raw item; return its event_id as sent and either the parsed record or an error message."""
    raw_id = item.get("event_id") if isinstance(item, dict) else None
    event_id = raw_id if isinstance(raw_id, str) else None
    try:
        return event_id, RecordIn.model_validate(item)
    except ValidationError as exc:
        return event_id, format_errors(exc)
