from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .database import now

class AuthRegister(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=6, max_length=200)
    password: str = Field(min_length=10, max_length=200)
    role: Literal["donor", "recipient"]

    @model_validator(mode="after")
    def validate_email(self):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.email):
            raise ValueError("Enter a valid email address")
        return self


class AuthLogin(BaseModel):
    email: str
    password: str


class RecipientIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    organization_type: Literal["ngo", "shelter", "community_kitchen"] = "ngo"
    contact: str = Field(min_length=5, max_length=60)
    address: str = Field(min_length=4, max_length=240)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    capacity_kg: float = Field(gt=0, le=100000)
    categories: list[str] = Field(min_length=1)
    storage: list[str] = Field(min_length=1)
    hours: str = Field(min_length=2, max_length=120)
    evidence: str = Field(min_length=10, max_length=4000)
    reference_id: str = Field(default="", max_length=120)
    darpan_id: str = Field(default="", max_length=32)
    pan_number: str = Field(default="", max_length=20)
    fcra_or_80g: str = Field(default="", max_length=120)
    daily_meal_capacity: int | None = Field(default=None, ge=0, le=100000)


class DonationIn(BaseModel):
    donor_name: str = Field(min_length=2, max_length=120)
    food_name: str = Field(min_length=2, max_length=120)
    category: Literal["veg", "non-veg"]
    quantity_kg: float = Field(gt=0, le=10000)
    prepared_at: datetime
    expires_at: datetime
    storage: Literal["hot", "ambient", "chilled"]
    temperature_c: float | None = Field(default=None, ge=-50, le=150)
    pickup_address: str = Field(min_length=4, max_length=240)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    notes: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def dates_make_sense(self):
        prepared = self.prepared_at.replace(tzinfo=timezone.utc) if self.prepared_at.tzinfo is None else self.prepared_at
        expires = self.expires_at.replace(tzinfo=timezone.utc) if self.expires_at.tzinfo is None else self.expires_at
        if prepared > now() + timedelta(minutes=5):
            raise ValueError("Preparation time cannot be in the future")
        if expires <= now():
            raise ValueError("Consumption deadline must be in the future")
        if expires <= prepared:
            raise ValueError("Consumption deadline must be after preparation time")
        if self.storage in ("hot", "chilled") and self.temperature_c is None:
            raise ValueError("Enter a food temperature for hot-held or chilled food")
        return self


class DonationRequestIn(BaseModel):
    quantity_kg: float = Field(gt=0, le=10000)


class ReviewIn(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=1000)


class ConfirmIn(BaseModel):
    step: Literal["pickup", "receipt"]
    code: str = ""

    @model_validator(mode="after")
    def pickup_requires_code(self):
        if self.step == "pickup":
            self.code = self.code.strip().upper()
            if not re.fullmatch(r"[A-F0-9]{16}", self.code):
                raise ValueError("Enter the 16-character delivery code")
        return self
