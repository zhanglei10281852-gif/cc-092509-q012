"""许可义务登记与结算准备的领域服务。

核心规则：
- 合同修改登记为新版本，允许追溯生效；窗口适用版本按“生效日不晚于
  窗口结束日的最近版本”确定，已锁定窗口永远保持结算时的版本。
- 对方报告按 (window_id, idempotency_key) 幂等导入，重复导入返回原
  结果且不重复计提；同一幂等键内容不同视为冲突。
- 计提 = max(0, 报告销售额 - 里程碑阈值) × 费率，按分位四舍五入；
  重算时旧计提标记 superseded 保留历史，不物理删除。
- 缺项与异常项生成待复核差异，差异清零后窗口才能锁定；锁定周期不
  可再导入报告、修正或重算。
- 合同暂停/终止只作废生效日之后开始的未结算窗口，不回滚历史。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.core.security import Principal
from app.licensing.repository import (
    LicenseAccrualRepository,
    LicenseAdjustmentRepository,
    LicenseContractRepository,
    LicenseDifferenceRepository,
    LicenseObligationRepository,
    LicenseReportRepository,
    LicenseVersionRepository,
    LicenseWindowRepository,
)
from app.services.audit import AuditService

_CENT = Decimal("0.01")


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _obligation_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (item["territory"], item["product_line"], item["milestone_code"])


def _ensure_unique_lines(items: list[dict[str, Any]], label: str) -> None:
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = _obligation_key(item)
        if key in seen:
            raise ValidationError(f"{label}存在重复维度：{'/'.join(key)}")
        seen.add(key)


class _LicensingBase:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.contracts = LicenseContractRepository(connection)
        self.versions = LicenseVersionRepository(connection)
        self.obligations = LicenseObligationRepository(connection)
        self.windows = LicenseWindowRepository(connection)
        self.reports = LicenseReportRepository(connection)
        self.accruals = LicenseAccrualRepository(connection)
        self.adjustments = LicenseAdjustmentRepository(connection)
        self.differences = LicenseDifferenceRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def _recompute_window(
        self,
        window: dict[str, Any],
        report: dict[str, Any],
        version: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """按指定合同版本与对方报告重算窗口计提，并同步待复核差异。"""
        obligations = self.obligations.list_for_version(version["id"])
        lines = self.reports.lines(report["id"])
        line_by_key = {_obligation_key(line): line for line in lines}
        obligation_keys = {_obligation_key(item) for item in obligations}

        self.accruals.supersede_window(window["id"])
        accrued_total = Decimal("0")
        missing: list[dict[str, Any]] = []
        for obligation in obligations:
            line = line_by_key.get(_obligation_key(obligation))
            reported = _money(line["reported_sales"]) if line else _money(0)
            base = max(reported - _money(obligation["threshold_amount"]), Decimal("0"))
            amount = _money(base * Decimal(str(obligation["royalty_rate"])))
            accrued_total += amount
            self.accruals.create(
                window["id"],
                obligation,
                report["id"],
                line["id"] if line else None,
                float(reported),
                float(amount),
                now,
            )
            if line is None:
                missing.append(obligation)
        unexpected = [line for key, line in line_by_key.items() if key not in obligation_keys]
        self._sync_differences(window, report, missing, unexpected, now)

        reported_total = sum((_money(line["reported_sales"]) for line in lines), Decimal("0"))
        adjustment_total = _money(self.adjustments.total_for_window(window["id"]))
        payable = accrued_total + adjustment_total
        pending = self.differences.pending_for_window(window["id"])
        return self.windows.apply_accrual_result(
            window["id"],
            status="reconciling" if pending else "reported",
            payable_status="accrued",
            contract_version_id=version["id"],
            report_id=report["id"],
            reported_amount=float(reported_total),
            accrued_amount=float(accrued_total),
            adjustment_amount=float(adjustment_total),
            payable_amount=float(payable),
            now=now,
        )

    def _sync_differences(
        self,
        window: dict[str, Any],
        report: dict[str, Any],
        missing: list[dict[str, Any]],
        unexpected: list[dict[str, Any]],
        now: str,
    ) -> None:
        """让待复核差异与最新计提结果对齐。

        已修复的差异自动核销；仍然存在的保留原记录不重复生成；人工已
        处理的差异不因重算复活。
        """
        missing_ids = {item["id"] for item in missing}
        unexpected_keys = {_obligation_key(line) for line in unexpected}
        open_missing: set[int] = set()
        open_unexpected: set[tuple[str, str, str]] = set()
        for difference in self.differences.pending_for_window(window["id"]):
            if difference["kind"] == "missing_item":
                if difference["obligation_id"] in missing_ids:
                    open_missing.add(difference["obligation_id"])
                else:
                    self.differences.resolve(difference["id"], "重新计提后缺项已消除", None, now)
            else:
                key = _obligation_key(difference["actual"])
                if key in unexpected_keys:
                    open_unexpected.add(key)
                else:
                    self.differences.resolve(difference["id"], "重新计提后异常项已消除", None, now)
        for obligation in missing:
            if obligation["id"] in open_missing:
                continue
            self.differences.create(
                window["id"],
                report["id"],
                "missing_item",
                obligation_id=obligation["id"],
                expected={
                    "territory": obligation["territory"],
                    "product_line": obligation["product_line"],
                    "milestone_code": obligation["milestone_code"],
                    "threshold_amount": obligation["threshold_amount"],
                    "royalty_rate": obligation["royalty_rate"],
                },
                actual={},
                now=now,
            )
        for line in unexpected:
            if _obligation_key(line) in open_unexpected:
                continue
            self.differences.create(
                window["id"],
                report["id"],
                "unexpected_item",
                obligation_id=None,
                expected={},
                actual={
                    "territory": line["territory"],
                    "product_line": line["product_line"],
                    "milestone_code": line["milestone_code"],
                    "reported_sales": line["reported_sales"],
                },
                now=now,
            )


class LicenseContractService(_LicensingBase):
    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licenses.write")
        _ensure_unique_lines(data["obligations"], "许可义务")
        if self.contracts.by_code(data["contract_code"]):
            raise ConflictError("合同编号已经存在")
        now = to_storage(self.clock.now())
        contract = self.contracts.create(data, now)
        version = self.versions.create(contract["id"], data, 1, principal.user_id, now)
        obligations = self.obligations.create_many(version["id"], data["obligations"], now)
        self.audit.record(
            principal,
            "license.contract.create",
            "license_contract",
            str(contract["id"]),
            after=contract,
            metadata={"version_no": 1, "obligation_count": len(obligations)},
        )
        return {"contract": contract, "version": version, "obligations": obligations}

    def list(self, principal: Principal, status: str | None) -> list[dict[str, Any]]:
        principal.require("licenses.read")
        return self.contracts.list(status)

    def detail(self, principal: Principal, contract_id: int) -> dict[str, Any]:
        principal.require("licenses.read")
        contract = self.contracts.get(contract_id)
        versions = []
        for version in self.versions.list_for_contract(contract_id):
            versions.append({**version, "obligations": self.obligations.list_for_version(version["id"])})
        return {
            "contract": contract,
            "versions": versions,
            "windows": self.windows.list_for_contract(contract_id),
        }

    def register_version(self, principal: Principal, contract_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licenses.write")
        _ensure_unique_lines(data["obligations"], "许可义务")
        contract = self.contracts.get(contract_id)
        if contract["status"] == "terminated":
            raise ConflictError("合同已终止，不能登记新版本")
        now = to_storage(self.clock.now())
        version = self.versions.create(contract_id, data, self.versions.next_version_no(contract_id), principal.user_id, now)
        obligations = self.obligations.create_many(version["id"], data["obligations"], now)
        restated = self._restate_open_windows(principal, contract, now)
        self.audit.record(
            principal,
            "license.version.register",
            "license_contract",
            str(contract_id),
            after=version,
            metadata={
                "version_no": version["version_no"],
                "effective_from": version["effective_from"],
                "restated_window_ids": [item["id"] for item in restated],
            },
        )
        return {"version": version, "obligations": obligations, "restated_windows": restated}

    def change_status(self, principal: Principal, contract_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licenses.write")
        contract = self.contracts.get(contract_id)
        action = data["action"]
        if action == "suspend":
            if contract["status"] != "active":
                raise ConflictError("只有生效中的合同可以暂停")
            target, label = "suspended", "暂停"
        elif action == "resume":
            if contract["status"] != "suspended":
                raise ConflictError("只有已暂停的合同可以恢复")
            target, label = "active", "恢复"
        else:
            if contract["status"] == "terminated":
                raise ConflictError("合同已经终止")
            target, label = "terminated", "终止"
        now = to_storage(self.clock.now())
        updated = self.contracts.set_status(contract_id, target, data["reason"], data["effective_from"], now)
        voided = []
        if target in {"suspended", "terminated"}:
            voided = self.windows.void_future(
                contract_id,
                data["effective_from"],
                f"合同{label}自 {data['effective_from']} 起：{data['reason']}",
                now,
            )
        self.audit.record(
            principal,
            "license.contract.status",
            "license_contract",
            str(contract_id),
            before=contract,
            after=updated,
            metadata={"action": action, "effective_from": data["effective_from"], "voided_window_ids": [item["id"] for item in voided]},
        )
        return {"contract": updated, "voided_windows": voided}

    def _restate_open_windows(self, principal: Principal, contract: dict[str, Any], now: str) -> list[dict[str, Any]]:
        """让未锁定窗口适用最新生效版本；已锁定周期保持结算时版本不变。"""
        restated = []
        for window in self.windows.restatable(contract["id"]):
            version = self.versions.applicable_for(contract["id"], window["window_end"])
            if version is None or window["contract_version_id"] == version["id"]:
                continue
            if window["report_id"] is None:
                self.windows.rebind_version(window["id"], version["id"], now)
                continue
            report = self.reports.get(window["report_id"])
            updated = self._recompute_window(window, report, version, now)
            self.audit.record(
                principal,
                "license.window.restate",
                "license_report_window",
                str(window["id"]),
                before=window,
                after=updated,
                metadata={"contract_version_id": version["id"], "report_id": report["id"]},
            )
            restated.append(updated)
        return restated


class LicenseWindowService(_LicensingBase):
    def create(self, principal: Principal, contract_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licenses.write")
        contract = self.contracts.get(contract_id)
        if contract["status"] != "active":
            raise ConflictError("合同暂停或终止时不能新增报告窗口")
        if data["window_end"] < data["window_start"]:
            raise ValidationError("窗口结束日不能早于开始日")
        if self.windows.by_label(contract_id, data["period_label"]):
            raise ConflictError("该合同下已存在同名报告窗口")
        if self.windows.overlapping(contract_id, data["window_start"], data["window_end"]):
            raise ConflictError("报告窗口与已有窗口期间重叠")
        now = to_storage(self.clock.now())
        version = self.versions.applicable_for(contract_id, data["window_end"])
        window = self.windows.create(contract_id, data, version["id"] if version else None, now)
        self.audit.record(principal, "license.window.create", "license_report_window", str(window["id"]), after=window)
        return window

    def list_for_contract(
        self,
        principal: Principal,
        contract_id: int,
        status: str | None,
        payable_status: str | None,
    ) -> list[dict[str, Any]]:
        principal.require("licenses.read")
        self.contracts.get(contract_id)
        return self.windows.list_for_contract(contract_id, status=status, payable_status=payable_status)

    def detail(self, principal: Principal, window_id: int) -> dict[str, Any]:
        principal.require("licenses.read")
        window = self.windows.get(window_id)
        return {
            "window": window,
            "accruals": self.accruals.active_for_window(window_id),
            "differences": self.differences.list_for_window(window_id),
            "adjustments": self.adjustments.list_for_window(window_id),
            "report": self.reports.get(window["report_id"]) if window["report_id"] else None,
        }

    def payables(self, principal: Principal, contract_id: int | None, payable_status: str | None) -> list[dict[str, Any]]:
        principal.require("licenses.read")
        return self.windows.list_payables(contract_id, payable_status)


class LicenseReportService(_LicensingBase):
    def import_report(self, principal: Principal, window_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licenses.write")
        _ensure_unique_lines(data["lines"], "报告明细")
        window = self.windows.get(window_id)
        contract = self.contracts.get(window["contract_id"])
        if contract["status"] == "terminated":
            raise ConflictError("合同已终止，不能再导入对方报告")
        if window["status"] == "locked":
            raise ConflictError("报告窗口已锁定，不能改变已结算周期")
        if window["status"] == "void":
            raise ConflictError("报告窗口已作废")
        content_hash = self._content_hash(data)
        existing = self.reports.by_idempotency_key(window_id, data["idempotency_key"])
        if existing:
            if existing["content_hash"] != content_hash:
                raise ConflictError("同一幂等键的报告内容不一致")
            return {
                "report": self.reports.get(existing["id"]),
                "window": self.windows.get(window_id),
                "replayed": True,
            }
        version = self.versions.applicable_for(contract["id"], window["window_end"])
        if version is None:
            raise ConflictError("该报告窗口没有已生效的合同版本")
        now = to_storage(self.clock.now())
        report = self.reports.create(window_id, contract["id"], data, content_hash, now)
        self.reports.add_lines(report["id"], data["lines"], now)
        updated = self._recompute_window(window, report, version, now)
        self.audit.record(
            principal,
            "license.report.import",
            "license_report_window",
            str(window_id),
            before=window,
            after=updated,
            metadata={"report_id": report["id"], "report_code": data["report_code"], "contract_version_id": version["id"]},
        )
        return {"report": self.reports.get(report["id"]), "window": updated, "replayed": False}

    def _content_hash(self, data: dict[str, Any]) -> str:
        canonical = json.dumps(
            {
                "report_code": data["report_code"],
                "submitted_by": data["submitted_by"],
                "submitted_at": data["submitted_at"],
                "lines": sorted(
                    (
                        {
                            "territory": line["territory"],
                            "product_line": line["product_line"],
                            "milestone_code": line["milestone_code"],
                            "reported_sales": line["reported_sales"],
                        }
                        for line in data["lines"]
                    ),
                    key=lambda item: (item["territory"], item["product_line"], item["milestone_code"]),
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LicenseSettlementService(_LicensingBase):
    def adjust(self, principal: Principal, window_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licenses.write")
        window = self.windows.get(window_id)
        self._ensure_mutable(window)
        if data.get("obligation_id"):
            obligation = self.obligations.get(data["obligation_id"])
            if obligation["contract_version_id"] != window["contract_version_id"]:
                raise ValidationError("修正的义务不属于窗口当前适用的合同版本")
        now = to_storage(self.clock.now())
        adjustment = self.adjustments.create(window_id, data, principal.user_id, now)
        adjustment_total = _money(self.adjustments.total_for_window(window_id))
        payable = _money(window["accrued_amount"]) + adjustment_total
        updated = self.windows.refresh_payable(window_id, float(adjustment_total), float(payable), now)
        self.audit.record(
            principal,
            "license.adjustment.create",
            "license_report_window",
            str(window_id),
            before=window,
            after=updated,
            metadata={"adjustment_id": adjustment["id"], "reason": data["reason"]},
        )
        return {"adjustment": adjustment, "window": updated}

    def resolve_difference(self, principal: Principal, window_id: int, difference_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licenses.write")
        window = self.windows.get(window_id)
        self._ensure_mutable(window)
        difference = self.differences.get(difference_id)
        if difference["window_id"] != window_id:
            raise ValidationError("差异不属于该报告窗口")
        if difference["state"] != "pending":
            raise ConflictError("差异已经处理")
        now = to_storage(self.clock.now())
        resolved = self.differences.resolve(difference_id, data["resolution_note"], principal.user_id, now)
        updated = window
        if window["status"] == "reconciling" and not self.differences.pending_for_window(window_id):
            updated = self.windows.set_status(window_id, "reported", now)
        self.audit.record(
            principal,
            "license.difference.resolve",
            "license_report_window",
            str(window_id),
            metadata={"difference_id": difference_id, "resolution_note": data["resolution_note"]},
        )
        return {"difference": resolved, "window": updated}

    def lock(self, principal: Principal, window_id: int) -> dict[str, Any]:
        principal.require("licenses.settle")
        window = self.windows.get(window_id)
        if window["status"] == "locked":
            raise ConflictError("报告窗口已经锁定")
        if window["status"] == "void":
            raise ConflictError("报告窗口已作废")
        if window["report_id"] is None:
            raise ConflictError("窗口尚未收到对方报告，不能锁定")
        pending = self.differences.pending_for_window(window_id)
        if pending:
            raise ConflictError(
                "仍有待复核差异，不能锁定周期",
                context={"difference_ids": [item["id"] for item in pending]},
            )
        now = to_storage(self.clock.now())
        locked = self.windows.lock(window_id, now)
        self.audit.record(
            principal,
            "license.window.lock",
            "license_report_window",
            str(window_id),
            before=window,
            after=locked,
            metadata={"payable_amount": locked["payable_amount"], "contract_version_id": locked["contract_version_id"]},
        )
        return locked

    def settle(self, principal: Principal, window_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licenses.settle")
        window = self.windows.get(window_id)
        if window["status"] != "locked":
            raise ConflictError("只有已锁定的周期可以登记支付")
        if window["payable_status"] == "paid":
            raise ConflictError("该周期已登记支付")
        now = to_storage(self.clock.now())
        settled = self.windows.settle(window_id, data["paid_at"], data["payment_reference"], now)
        self.audit.record(
            principal,
            "license.window.settle",
            "license_report_window",
            str(window_id),
            before=window,
            after=settled,
            metadata={"payment_reference": data["payment_reference"]},
        )
        return settled

    def trace(self, principal: Principal, window_id: int) -> dict[str, Any]:
        """从一笔待付金额回溯合同版本、原始报告与人工修正理由。"""
        principal.require("licenses.read")
        window = self.windows.get(window_id)
        contract = self.contracts.get(window["contract_id"])
        version = self.versions.get(window["contract_version_id"]) if window["contract_version_id"] else None
        report = self.reports.get(window["report_id"]) if window["report_id"] else None
        return {
            "window": window,
            "contract": contract,
            "contract_version": (
                {**version, "obligations": self.obligations.list_for_version(version["id"])} if version else None
            ),
            "report": report,
            "accruals": self.accruals.active_for_window(window_id),
            "adjustments": self.adjustments.list_for_window(window_id),
            "differences": self.differences.list_for_window(window_id),
        }

    def _ensure_mutable(self, window: dict[str, Any]) -> None:
        if window["status"] == "locked":
            raise ConflictError("报告窗口已锁定，不能改变已结算周期")
        if window["status"] == "void":
            raise ConflictError("报告窗口已作废")
