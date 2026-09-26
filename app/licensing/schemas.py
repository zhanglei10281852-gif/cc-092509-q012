from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LicenseAssetInput(BaseModel):
    asset_ref: str = Field(min_length=2, max_length=100)
    dossier_id: int | None = Field(default=None, gt=0)


class LicenseTerms(BaseModel):
    currency: str = Field(min_length=3, max_length=8)
    royalty_rate: float = Field(ge=0, le=1)
    territories: list[str] = Field(min_length=1, max_length=50)
    product_lines: list[str] = Field(min_length=1, max_length=50)
    milestone_payments: dict[str, float] = Field(default_factory=dict)


class ContractCreate(BaseModel):
    contract_code: str = Field(min_length=3, max_length=64)
    licensee_name: str = Field(min_length=2, max_length=200)
    effective_from: str = Field(min_length=10, max_length=40)
    terms: LicenseTerms
    assets: list[LicenseAssetInput] = Field(min_length=1, max_length=200)
    change_reason: str = Field(default="初始签订", max_length=500)


class VersionCreate(BaseModel):
    effective_from: str = Field(min_length=10, max_length=40)
    change_reason: str = Field(min_length=2, max_length=500)
    terms: LicenseTerms
    assets: list[LicenseAssetInput] | None = Field(default=None, min_length=1, max_length=200)


class ContractStatusChange(BaseModel):
    action: Literal["suspend", "resume", "terminate"]
    effective_from: str = Field(min_length=10, max_length=40)
    reason: str = Field(min_length=2, max_length=500)


class WindowCreate(BaseModel):
    window_code: str = Field(min_length=2, max_length=64)
    period_start: str = Field(min_length=10, max_length=40)
    period_end: str = Field(min_length=10, max_length=40)
    due_at: str = Field(min_length=10, max_length=40)
    threshold_amount: float = Field(default=0, ge=0)


class ReportLineInput(BaseModel):
    territory: str = Field(min_length=1, max_length=50)
    product_line: str = Field(min_length=1, max_length=50)
    milestone: str | None = Field(default=None, max_length=50)
    gross_sales: float = Field(ge=0)


class ReportCreate(BaseModel):
    report_code: str = Field(min_length=3, max_length=64)
    window_id: int = Field(gt=0)
    lines: list[ReportLineInput] = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=500)


class DiscrepancyResolve(BaseModel):
    status: Literal["resolved", "waived"] = "resolved"
    note: str = Field(min_length=2, max_length=500)


class AdjustmentCreate(BaseModel):
    delta_amount: float
    reason: str = Field(min_length=2, max_length=500)
