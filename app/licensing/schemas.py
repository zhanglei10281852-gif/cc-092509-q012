"""专利许可合同登记与结算准备的请求模型。

合同版本、报告窗口与对方报告均来自线下流转文件，字段校验集中在
这里，保证导入与重放时得到稳定的错误信息。日期一律使用 ISO 日历日
(YYYY-MM-DD)，与报告窗口的自然期间语义保持一致。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator


def _require_date(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是 YYYY-MM-DD 格式的日期") from exc
    return value


class ObligationInput(BaseModel):
    territory: str = Field(min_length=1, max_length=100)
    product_line: str = Field(min_length=1, max_length=100)
    milestone_code: str = Field(min_length=1, max_length=50)
    threshold_amount: float = Field(ge=0)
    royalty_rate: float = Field(gt=0, le=1)
    asset_codes: list[str] = Field(min_length=1, max_length=50)

    @field_validator("asset_codes")
    @classmethod
    def _asset_codes_not_blank(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 100 for item in cleaned):
            raise ValueError("适用资产编码不能为空且不超过 100 字符")
        return cleaned


class ContractCreate(BaseModel):
    contract_code: str = Field(min_length=3, max_length=64)
    licensee_name: str = Field(min_length=1, max_length=200)
    licensee_code: str = Field(min_length=1, max_length=64)
    effective_from: str = Field(min_length=10, max_length=10)
    currency: str = Field(default="CNY", min_length=3, max_length=8)
    change_reason: str = Field(default="合同签订", max_length=500)
    obligations: list[ObligationInput] = Field(min_length=1, max_length=200)

    @field_validator("effective_from")
    @classmethod
    def _effective_from_is_date(cls, value: str) -> str:
        return _require_date(value, "生效日期")


class ContractVersionCreate(BaseModel):
    effective_from: str = Field(min_length=10, max_length=10)
    currency: str = Field(default="CNY", min_length=3, max_length=8)
    change_reason: str = Field(min_length=2, max_length=500)
    obligations: list[ObligationInput] = Field(min_length=1, max_length=200)

    @field_validator("effective_from")
    @classmethod
    def _effective_from_is_date(cls, value: str) -> str:
        return _require_date(value, "生效日期")


class ContractStatusChange(BaseModel):
    action: str = Field(pattern="^(suspend|resume|terminate)$")
    effective_from: str = Field(min_length=10, max_length=10)
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("effective_from")
    @classmethod
    def _effective_from_is_date(cls, value: str) -> str:
        return _require_date(value, "生效日期")


class WindowCreate(BaseModel):
    period_label: str = Field(min_length=2, max_length=50)
    window_start: str = Field(min_length=10, max_length=10)
    window_end: str = Field(min_length=10, max_length=10)
    due_at: str = Field(min_length=10, max_length=10)

    @field_validator("window_start", "window_end", "due_at")
    @classmethod
    def _is_date(cls, value: str) -> str:
        return _require_date(value, "报告窗口日期")


class ReportLineInput(BaseModel):
    territory: str = Field(min_length=1, max_length=100)
    product_line: str = Field(min_length=1, max_length=100)
    milestone_code: str = Field(min_length=1, max_length=50)
    reported_sales: float = Field(ge=0)


class ReportImport(BaseModel):
    report_code: str = Field(min_length=2, max_length=100)
    idempotency_key: str = Field(min_length=4, max_length=100)
    submitted_by: str = Field(min_length=1, max_length=100)
    submitted_at: str = Field(min_length=10, max_length=40)
    lines: list[ReportLineInput] = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=500)


class AdjustmentCreate(BaseModel):
    obligation_id: int | None = Field(default=None, gt=0)
    delta_amount: float
    reason: str = Field(min_length=4, max_length=500)

    @field_validator("delta_amount")
    @classmethod
    def _delta_not_zero(cls, value: float) -> float:
        if value == 0:
            raise ValueError("修正金额不能为零")
        return value


class DifferenceResolve(BaseModel):
    resolution_note: str = Field(min_length=2, max_length=500)


class WindowSettle(BaseModel):
    paid_at: str = Field(min_length=10, max_length=10)
    payment_reference: str = Field(min_length=2, max_length=100)

    @field_validator("paid_at")
    @classmethod
    def _paid_at_is_date(cls, value: str) -> str:
        return _require_date(value, "支付日期")
