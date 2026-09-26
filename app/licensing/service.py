from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditService


def _row_dict(row: sqlite3.Row | None, message: str = "记录不存在") -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return dict(row)


def _report_digest(contract_id: int, data: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "contract_id": contract_id,
            "report_code": data["report_code"],
            "window_id": data["window_id"],
            "note": data.get("note", ""),
            "lines": sorted(
                (
                    {
                        "territory": line["territory"],
                        "product_line": line["product_line"],
                        "milestone": line.get("milestone") or "",
                        "gross_sales": line["gross_sales"],
                    }
                    for line in data["lines"]
                ),
                key=lambda item: (item["territory"], item["product_line"], item["milestone"]),
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LicenseContractService:
    """许可合同登记、版本修订与暂停/终止。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    def _contract(self, contract_id: int) -> dict[str, Any]:
        return _row_dict(
            self.connection.execute("SELECT * FROM license_contracts WHERE id=?", (contract_id,)).fetchone(),
            "许可合同不存在",
        )

    def _append_event(self, contract_id: int, event_type: str, actor_user_id: int, now: str, details: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO license_contract_events(contract_id,event_type,actor_user_id,details_json,occurred_at) VALUES(?,?,?,?,?)",
            (contract_id, event_type, actor_user_id, json.dumps(details, ensure_ascii=False), now),
        )

    def _insert_version(
        self,
        contract_id: int,
        version_no: int,
        data: dict[str, Any],
        assets: list[dict[str, Any]],
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO license_contract_versions(contract_id,version_no,effective_from,change_reason,terms_json,created_by,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                contract_id,
                version_no,
                data["effective_from"],
                data["change_reason"],
                json.dumps(data["terms"], ensure_ascii=False, sort_keys=True),
                actor_user_id,
                now,
            ),
        )
        version_id = cursor.lastrowid
        for asset in assets:
            self.connection.execute(
                "INSERT INTO license_contract_assets(version_id,asset_ref,dossier_id) VALUES(?,?,?)",
                (version_id, asset["asset_ref"], asset.get("dossier_id")),
            )
        return self._version(version_id)

    def _version(self, version_id: int) -> dict[str, Any]:
        version = _row_dict(
            self.connection.execute("SELECT * FROM license_contract_versions WHERE id=?", (version_id,)).fetchone(),
            "合同版本不存在",
        )
        version["terms"] = json.loads(version.pop("terms_json"))
        version["assets"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM license_contract_assets WHERE version_id=? ORDER BY asset_ref", (version_id,)
            ).fetchall()
        ]
        return version

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licensing.write")
        existing = self.connection.execute(
            "SELECT id FROM license_contracts WHERE contract_code=?", (data["contract_code"],)
        ).fetchone()
        if existing:
            raise ConflictError("合同编号已经存在")
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """INSERT INTO license_contracts(contract_code,licensee_name,status,effective_from,created_at,updated_at)
               VALUES(?,?, 'active', ?,?,?)""",
            (data["contract_code"], data["licensee_name"], data["effective_from"], now, now),
        )
        contract_id = cursor.lastrowid
        version = self._insert_version(contract_id, 1, data, data["assets"], principal.user_id, now)
        self._append_event(contract_id, "created", principal.user_id, now, {"contract_code": data["contract_code"], "version_no": 1})
        contract = self._contract(contract_id)
        self.audit.record(principal, "license_contract.create", "license_contract", str(contract_id), after=contract)
        return {"contract": contract, "current_version": version}

    def amend(self, principal: Principal, contract_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licensing.write")
        contract = self._contract(contract_id)
        if contract["status"] == "terminated":
            raise ConflictError("合同已终止，不能修改")
        locked_end = self.connection.execute(
            "SELECT MAX(period_end) FROM license_report_windows WHERE contract_id=? AND status='locked'",
            (contract_id,),
        ).fetchone()[0]
        if locked_end and data["effective_from"] <= locked_end:
            raise ConflictError(
                "合同修改不能改变已经结算的周期",
                context={"locked_period_end": locked_end, "effective_from": data["effective_from"]},
            )
        latest = _row_dict(
            self.connection.execute(
                "SELECT * FROM license_contract_versions WHERE contract_id=? ORDER BY version_no DESC LIMIT 1",
                (contract_id,),
            ).fetchone(),
            "合同版本不存在",
        )
        if data["effective_from"] <= latest["effective_from"]:
            raise ValidationError("新版本生效日期必须晚于现有最新版本的生效日期")
        assets = data.get("assets")
        if assets is None:
            assets = [
                {"asset_ref": row["asset_ref"], "dossier_id": row["dossier_id"]}
                for row in self.connection.execute(
                    "SELECT asset_ref,dossier_id FROM license_contract_assets WHERE version_id=? ORDER BY asset_ref",
                    (latest["id"],),
                ).fetchall()
            ]
        now = to_storage(self.clock.now())
        version = self._insert_version(contract_id, latest["version_no"] + 1, data, assets, principal.user_id, now)
        self.connection.execute(
            "UPDATE license_contracts SET version=version+1,updated_at=? WHERE id=?",
            (now, contract_id),
        )
        self._append_event(
            contract_id,
            "amended",
            principal.user_id,
            now,
            {"version_no": version["version_no"], "effective_from": data["effective_from"], "change_reason": data["change_reason"]},
        )
        self.audit.record(
            principal,
            "license_contract.amend",
            "license_contract",
            str(contract_id),
            before={"latest_version_no": latest["version_no"]},
            after={"latest_version_no": version["version_no"], "effective_from": data["effective_from"]},
        )
        return {"contract": self._contract(contract_id), "current_version": version}

    def change_status(self, principal: Principal, contract_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licensing.write")
        contract = self._contract(contract_id)
        action = data["action"]
        effective_from = data["effective_from"]
        now = to_storage(self.clock.now())
        voided_window_ids: list[int] = []
        if action == "suspend":
            if contract["status"] != "active":
                raise ConflictError("仅生效中的合同可以暂停")
            new_status, effective_to = "suspended", contract["effective_to"]
        elif action == "resume":
            if contract["status"] != "suspended":
                raise ConflictError("仅已暂停的合同可以恢复")
            new_status, effective_to = "active", contract["effective_to"]
        else:
            if contract["status"] == "terminated":
                raise ConflictError("合同已经终止")
            new_status, effective_to = "terminated", effective_from
        if action in {"suspend", "terminate"}:
            # 只作废生效日之后尚未开始结算的开放窗口，已报告/已确认/已锁定的历史周期保持不变
            voided_window_ids = [
                row["id"]
                for row in self.connection.execute(
                    "SELECT id FROM license_report_windows WHERE contract_id=? AND status='open' AND period_start>=?",
                    (contract_id, effective_from),
                ).fetchall()
            ]
            if voided_window_ids:
                placeholders = ",".join("?" for _ in voided_window_ids)
                self.connection.execute(
                    f"UPDATE license_report_windows SET status='voided',updated_at=? WHERE id IN ({placeholders})",
                    (now, *voided_window_ids),
                )
        self.connection.execute(
            "UPDATE license_contracts SET status=?,effective_to=?,version=version+1,updated_at=? WHERE id=?",
            (new_status, effective_to, now, contract_id),
        )
        event_type = {"suspend": "suspended", "resume": "resumed", "terminate": "terminated"}[action]
        self._append_event(
            contract_id,
            event_type,
            principal.user_id,
            now,
            {"effective_from": effective_from, "reason": data["reason"], "voided_window_ids": voided_window_ids},
        )
        updated = self._contract(contract_id)
        self.audit.record(
            principal,
            f"license_contract.{action}",
            "license_contract",
            str(contract_id),
            before=contract,
            after=updated,
            metadata={"reason": data["reason"], "voided_window_ids": voided_window_ids},
        )
        return {"contract": updated, "voided_window_ids": voided_window_ids}

    def detail(self, principal: Principal, contract_id: int) -> dict[str, Any]:
        principal.require("licensing.read")
        contract = self._contract(contract_id)
        versions = [
            self._version(row["id"])
            for row in self.connection.execute(
                "SELECT id FROM license_contract_versions WHERE contract_id=? ORDER BY version_no", (contract_id,)
            ).fetchall()
        ]
        windows = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM license_report_windows WHERE contract_id=? ORDER BY period_start,id", (contract_id,)
            ).fetchall()
        ]
        events = []
        for row in self.connection.execute(
            "SELECT * FROM license_contract_events WHERE contract_id=? ORDER BY id", (contract_id,)
        ).fetchall():
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            events.append(item)
        return {"contract": contract, "versions": versions, "windows": windows, "events": events}

    def list(self, principal: Principal, status: str | None) -> list[dict[str, Any]]:
        principal.require("licensing.read")
        if status:
            rows = self.connection.execute(
                "SELECT * FROM license_contracts WHERE status=? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM license_contracts ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]


class LicenseSettlementService:
    """报告窗口、对方报告导入、差异复核、确认锁定与人工修正。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    def _contract(self, contract_id: int) -> dict[str, Any]:
        return _row_dict(
            self.connection.execute("SELECT * FROM license_contracts WHERE id=?", (contract_id,)).fetchone(),
            "许可合同不存在",
        )

    def _window(self, window_id: int) -> dict[str, Any]:
        return _row_dict(
            self.connection.execute("SELECT * FROM license_report_windows WHERE id=?", (window_id,)).fetchone(),
            "报告窗口不存在",
        )

    def _accrual(self, accrual_id: int) -> dict[str, Any]:
        return _row_dict(
            self.connection.execute("SELECT * FROM license_accruals WHERE id=?", (accrual_id,)).fetchone(),
            "计提记录不存在",
        )

    def create_window(self, principal: Principal, contract_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licensing.write")
        contract = self._contract(contract_id)
        if contract["status"] != "active":
            raise ConflictError("合同已暂停或终止，不能新增报告窗口")
        if data["period_start"] >= data["period_end"]:
            raise ValidationError("报告窗口开始日期必须早于结束日期")
        duplicate = self.connection.execute(
            "SELECT id FROM license_report_windows WHERE contract_id=? AND window_code=?",
            (contract_id, data["window_code"]),
        ).fetchone()
        if duplicate:
            raise ConflictError("报告窗口编码已经存在")
        overlap = self.connection.execute(
            """SELECT id,window_code FROM license_report_windows
               WHERE contract_id=? AND status!='voided' AND period_start<? AND period_end>?""",
            (contract_id, data["period_end"], data["period_start"]),
        ).fetchone()
        if overlap:
            raise ConflictError("报告窗口与已有窗口期间重叠", context={"conflict_window_code": overlap["window_code"]})
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """INSERT INTO license_report_windows(contract_id,window_code,period_start,period_end,due_at,threshold_amount,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?, 'open', ?,?)""",
            (
                contract_id,
                data["window_code"],
                data["period_start"],
                data["period_end"],
                data["due_at"],
                data.get("threshold_amount", 0),
                now,
                now,
            ),
        )
        window = self._window(cursor.lastrowid)
        self.audit.record(principal, "license_window.create", "license_report_window", str(window["id"]), after=window)
        return window

    def _version_for_window(self, contract_id: int, window: dict[str, Any]) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM license_contract_versions
               WHERE contract_id=? AND effective_from<=?
               ORDER BY effective_from DESC, version_no DESC LIMIT 1""",
            (contract_id, window["period_start"]),
        ).fetchone()
        return dict(row) if row else None

    def _report_payload(self, report_id: int, replayed: bool) -> dict[str, Any]:
        report = _row_dict(
            self.connection.execute("SELECT * FROM license_reports WHERE id=?", (report_id,)).fetchone(),
            "报告不存在",
        )
        lines = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM license_report_lines WHERE report_id=? ORDER BY line_no", (report_id,)
            ).fetchall()
        ]
        accruals = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM license_accruals WHERE report_id=? ORDER BY id", (report_id,)
            ).fetchall()
        ]
        discrepancies = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM license_discrepancies WHERE report_id=? ORDER BY id", (report_id,)
            ).fetchall()
        ]
        return {"report": report, "lines": lines, "accruals": accruals, "discrepancies": discrepancies, "replayed": replayed}

    def receive_report(self, principal: Principal, contract_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licensing.write")
        contract = self._contract(contract_id)
        digest = _report_digest(contract_id, data)
        existing = self.connection.execute(
            "SELECT * FROM license_reports WHERE contract_id=? AND report_code=?",
            (contract_id, data["report_code"]),
        ).fetchone()
        if existing:
            if existing["content_digest"] != digest:
                raise ConflictError("报告编号已被不同内容的报告占用")
            return self._report_payload(existing["id"], replayed=True)
        if contract["status"] == "suspended":
            raise ConflictError("合同已暂停，不能接收对方报告")
        if contract["status"] == "terminated":
            raise ConflictError("合同已终止，不能接收对方报告")
        window = self._window(data["window_id"])
        if window["contract_id"] != contract_id:
            raise ValidationError("报告窗口不属于该合同")
        if window["status"] == "voided":
            raise ConflictError("报告窗口已作废，不能导入报告")
        if window["status"] in {"confirmed", "locked"}:
            raise ConflictError("报告窗口已确认或锁定，不能导入新报告")
        if window["status"] == "reported":
            raise ConflictError("报告窗口已接收其他报告")
        version = self._version_for_window(contract_id, window)
        if version is None:
            raise ConflictError("报告窗口起始日期早于合同生效日期，无法确定适用合同版本")
        terms = json.loads(version["terms_json"])
        seen_dimensions: set[tuple[str, str, str]] = set()
        for line in data["lines"]:
            dimension = (line["territory"], line["product_line"], line.get("milestone") or "")
            if dimension in seen_dimensions:
                raise ValidationError("报告存在重复维度行", context={"dimension": "/".join(dimension)})
            seen_dimensions.add(dimension)
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """INSERT INTO license_reports(contract_id,window_id,report_code,content_digest,received_by,received_at,note,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (contract_id, window["id"], data["report_code"], digest, principal.user_id, now, data.get("note", ""), now),
        )
        report_id = cursor.lastrowid
        rate = float(terms["royalty_rate"])
        milestone_payments = {key: float(value) for key, value in terms.get("milestone_payments", {}).items()}
        discrepancies: list[tuple[str, str, str, str, str]] = []
        total_accrued = 0.0
        for line_no, line in enumerate(data["lines"], start=1):
            milestone = line.get("milestone") or ""
            cursor = self.connection.execute(
                """INSERT INTO license_report_lines(report_id,line_no,territory,product_line,milestone,gross_sales)
                   VALUES(?,?,?,?,?,?)""",
                (report_id, line_no, line["territory"], line["product_line"], milestone, line["gross_sales"]),
            )
            report_line_id = cursor.lastrowid
            royalty_amount = round(line["gross_sales"] * rate, 2)
            self.connection.execute(
                """INSERT INTO license_accruals(contract_id,window_id,report_id,report_line_id,contract_version_id,
                       accrual_kind,territory,product_line,milestone,basis_amount,royalty_rate,amount,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
                (
                    contract_id,
                    window["id"],
                    report_id,
                    report_line_id,
                    version["id"],
                    "royalty",
                    line["territory"],
                    line["product_line"],
                    milestone,
                    line["gross_sales"],
                    rate,
                    royalty_amount,
                    now,
                ),
            )
            total_accrued += royalty_amount
            if line["territory"] not in terms["territories"] or line["product_line"] not in terms["product_lines"]:
                discrepancies.append(
                    (
                        "unexpected_line",
                        line["territory"],
                        line["product_line"],
                        f"合同范围：地域 {sorted(terms['territories'])}，产品线 {sorted(terms['product_lines'])}",
                        f"{line['territory']}/{line['product_line']}",
                    )
                )
            if milestone:
                if milestone in milestone_payments:
                    milestone_amount = round(milestone_payments[milestone], 2)
                    self.connection.execute(
                        """INSERT INTO license_accruals(contract_id,window_id,report_id,report_line_id,contract_version_id,
                               accrual_kind,territory,product_line,milestone,basis_amount,royalty_rate,amount,status,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
                        (
                            contract_id,
                            window["id"],
                            report_id,
                            report_line_id,
                            version["id"],
                            "milestone",
                            line["territory"],
                            line["product_line"],
                            milestone,
                            0,
                            0,
                            milestone_amount,
                            now,
                        ),
                    )
                    total_accrued += milestone_amount
                else:
                    discrepancies.append(
                        (
                            "unexpected_line",
                            line["territory"],
                            line["product_line"],
                            f"合同版本未定义里程碑：{sorted(milestone_payments)}",
                            milestone,
                        )
                    )
        reported_dimensions = {(line["territory"], line["product_line"]) for line in data["lines"]}
        for territory in terms["territories"]:
            for product_line in terms["product_lines"]:
                if (territory, product_line) not in reported_dimensions:
                    discrepancies.append(
                        (
                            "missing_line",
                            territory,
                            product_line,
                            f"合同要求按 {territory}/{product_line} 报告",
                            "报告缺失该维度行",
                        )
                    )
        threshold = float(window["threshold_amount"])
        if threshold > 0 and total_accrued < threshold:
            discrepancies.append(
                (
                    "below_threshold",
                    "",
                    "",
                    f"窗口最低应付阈值 {threshold}",
                    f"本期计提合计 {round(total_accrued, 2)}",
                )
            )
        for discrepancy_type, territory, product_line, expected, actual in discrepancies:
            self.connection.execute(
                """INSERT INTO license_discrepancies(window_id,report_id,discrepancy_type,territory,product_line,
                       expected_value,actual_value,status,created_at)
                   VALUES(?,?,?,?,?,?,?, 'pending', ?)""",
                (window["id"], report_id, discrepancy_type, territory, product_line, expected, actual, now),
            )
        self.connection.execute(
            "UPDATE license_report_windows SET status='reported',updated_at=? WHERE id=?",
            (now, window["id"]),
        )
        payload = self._report_payload(report_id, replayed=False)
        self.audit.record(
            principal,
            "license_report.receive",
            "license_report",
            str(report_id),
            after={"report_code": data["report_code"], "window_id": window["id"]},
            metadata={
                "contract_version_id": version["id"],
                "accrual_count": len(payload["accruals"]),
                "discrepancy_count": len(discrepancies),
            },
        )
        return payload

    def resolve_discrepancy(self, principal: Principal, discrepancy_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licensing.write")
        discrepancy = _row_dict(
            self.connection.execute("SELECT * FROM license_discrepancies WHERE id=?", (discrepancy_id,)).fetchone(),
            "待复核差异不存在",
        )
        if discrepancy["status"] != "pending":
            raise ConflictError("差异已经处理完毕")
        window = self._window(discrepancy["window_id"])
        if window["status"] in {"locked", "voided"}:
            raise ConflictError("报告窗口已锁定或作废，不能处理差异")
        now = to_storage(self.clock.now())
        self.connection.execute(
            """UPDATE license_discrepancies SET status=?,resolution_note=?,resolved_by=?,resolved_at=? WHERE id=?""",
            (data["status"], data["note"], principal.user_id, now, discrepancy_id),
        )
        updated = _row_dict(
            self.connection.execute("SELECT * FROM license_discrepancies WHERE id=?", (discrepancy_id,)).fetchone()
        )
        self.audit.record(
            principal,
            "license_discrepancy.resolve",
            "license_discrepancy",
            str(discrepancy_id),
            before=discrepancy,
            after=updated,
        )
        return updated

    def confirm_window(self, principal: Principal, window_id: int) -> dict[str, Any]:
        principal.require("licensing.write")
        window = self._window(window_id)
        if window["status"] != "reported":
            raise ConflictError("仅已接收报告的窗口可以确认")
        pending = self.connection.execute(
            "SELECT COUNT(*) FROM license_discrepancies WHERE window_id=? AND status='pending'",
            (window_id,),
        ).fetchone()[0]
        if pending:
            raise ConflictError("存在待复核差异，不能确认周期", context={"pending_discrepancies": pending})
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE license_accruals SET status='confirmed' WHERE window_id=? AND status='pending'",
            (window_id,),
        )
        self.connection.execute(
            "UPDATE license_report_windows SET status='confirmed',updated_at=? WHERE id=?",
            (now, window_id),
        )
        updated = self._window(window_id)
        self.audit.record(principal, "license_window.confirm", "license_report_window", str(window_id), before=window, after=updated)
        return updated

    def lock_window(self, principal: Principal, window_id: int) -> dict[str, Any]:
        principal.require("licensing.lock")
        window = self._window(window_id)
        if window["status"] != "confirmed":
            raise ConflictError("仅已确认的窗口可以锁定")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE license_accruals SET status='locked' WHERE window_id=? AND status='confirmed'",
            (window_id,),
        )
        self.connection.execute(
            "UPDATE license_report_windows SET status='locked',updated_at=? WHERE id=?",
            (now, window_id),
        )
        updated = self._window(window_id)
        self.audit.record(principal, "license_window.lock", "license_report_window", str(window_id), before=window, after=updated)
        return updated

    def adjust_accrual(self, principal: Principal, accrual_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("licensing.write")
        accrual = self._accrual(accrual_id)
        if accrual["status"] == "locked":
            raise ConflictError("已锁定的计提不能人工修正")
        window = self._window(accrual["window_id"])
        if window["status"] in {"locked", "voided"}:
            raise ConflictError("报告窗口已锁定或作废，不能人工修正")
        delta = data["delta_amount"]
        if delta == 0:
            raise ValidationError("修正金额不能为零")
        adjusted_so_far = self.connection.execute(
            "SELECT COALESCE(SUM(delta_amount),0) FROM license_adjustments WHERE accrual_id=?",
            (accrual_id,),
        ).fetchone()[0]
        if accrual["amount"] + adjusted_so_far + delta < 0:
            raise ValidationError("修正后应付金额不能为负")
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO license_adjustments(accrual_id,delta_amount,reason,adjusted_by,created_at) VALUES(?,?,?,?,?)",
            (accrual_id, delta, data["reason"], principal.user_id, now),
        )
        if accrual["status"] == "confirmed":
            # 已确认的计提被人工修正后需要重新确认
            self.connection.execute(
                "UPDATE license_accruals SET status='pending' WHERE id=?",
                (accrual_id,),
            )
            self.connection.execute(
                "UPDATE license_report_windows SET status='reported',updated_at=? WHERE id=? AND status='confirmed'",
                (now, window["id"]),
            )
        adjustment = _row_dict(
            self.connection.execute("SELECT * FROM license_adjustments WHERE id=?", (cursor.lastrowid,)).fetchone()
        )
        self.audit.record(
            principal,
            "license_accrual.adjust",
            "license_accrual",
            str(accrual_id),
            after=adjustment,
            metadata={"reason": data["reason"], "delta_amount": delta},
        )
        return {"adjustment": adjustment, "accrual": self._accrual(accrual_id), "window": self._window(window["id"])}


class LicenseQueryService:
    """应付金额查询与从计提到合同版本、原始报告、人工修正的追溯。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def _window(self, window_id: int) -> dict[str, Any]:
        return _row_dict(
            self.connection.execute("SELECT * FROM license_report_windows WHERE id=?", (window_id,)).fetchone(),
            "报告窗口不存在",
        )

    def window_detail(self, principal: Principal, window_id: int) -> dict[str, Any]:
        principal.require("licensing.read")
        window = self._window(window_id)
        accruals = self._accruals_with_payable("a.window_id=?", (window_id,))
        discrepancies = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM license_discrepancies WHERE window_id=? ORDER BY id", (window_id,)
            ).fetchall()
        ]
        reports = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM license_reports WHERE window_id=? ORDER BY id", (window_id,)
            ).fetchall()
        ]
        return {"window": window, "accruals": accruals, "discrepancies": discrepancies, "reports": reports}

    def _accruals_with_payable(self, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"""SELECT a.*,
                       a.amount + COALESCE((SELECT SUM(delta_amount) FROM license_adjustments WHERE accrual_id=a.id),0) AS payable_amount,
                       w.window_code, c.contract_code
                FROM license_accruals a
                JOIN license_report_windows w ON w.id=a.window_id
                JOIN license_contracts c ON c.id=a.contract_id
                WHERE {where} ORDER BY a.id""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def payables(self, principal: Principal, contract_id: int, status: str | None) -> list[dict[str, Any]]:
        principal.require("licensing.read")
        if not self.connection.execute("SELECT id FROM license_contracts WHERE id=?", (contract_id,)).fetchone():
            raise NotFoundError("许可合同不存在")
        if status:
            return self._accruals_with_payable("a.contract_id=? AND a.status=?", (contract_id, status))
        return self._accruals_with_payable("a.contract_id=?", (contract_id,))

    def trace(self, principal: Principal, accrual_id: int) -> dict[str, Any]:
        principal.require("licensing.read")
        accrual_rows = self._accruals_with_payable("a.id=?", (accrual_id,))
        if not accrual_rows:
            raise NotFoundError("计提记录不存在")
        accrual = accrual_rows[0]
        contract = _row_dict(
            self.connection.execute("SELECT * FROM license_contracts WHERE id=?", (accrual["contract_id"],)).fetchone(),
            "许可合同不存在",
        )
        version = _row_dict(
            self.connection.execute("SELECT * FROM license_contract_versions WHERE id=?", (accrual["contract_version_id"],)).fetchone(),
            "合同版本不存在",
        )
        version["terms"] = json.loads(version.pop("terms_json"))
        version["assets"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM license_contract_assets WHERE version_id=? ORDER BY asset_ref", (version["id"],)
            ).fetchall()
        ]
        window = self._window(accrual["window_id"])
        report = _row_dict(
            self.connection.execute("SELECT * FROM license_reports WHERE id=?", (accrual["report_id"],)).fetchone(),
            "报告不存在",
        )
        report_line = None
        if accrual["report_line_id"]:
            report_line = _row_dict(
                self.connection.execute("SELECT * FROM license_report_lines WHERE id=?", (accrual["report_line_id"],)).fetchone(),
                "报告行不存在",
            )
        adjustments = [
            dict(row)
            for row in self.connection.execute(
                """SELECT adj.*,u.display_name AS adjusted_by_name
                   FROM license_adjustments adj LEFT JOIN users u ON u.id=adj.adjusted_by
                   WHERE adj.accrual_id=? ORDER BY adj.id""",
                (accrual_id,),
            ).fetchall()
        ]
        return {
            "accrual": accrual,
            "contract": contract,
            "contract_version": version,
            "window": window,
            "report": report,
            "report_line": report_line,
            "adjustments": adjustments,
        }
