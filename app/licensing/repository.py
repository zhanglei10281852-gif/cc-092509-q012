"""许可合同、报告窗口与结算准备的数据访问层。

所有金额在入库前已由服务层用 Decimal 量化到分，这里只负责持久化
与查询，不再做业务判断。窗口上的 contract_version_id / report_id 记录
当前计提所依据的合同版本与对方报告，供追溯查询直接取用。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import NotFoundError


def _row(row: sqlite3.Row | None, message: str) -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return dict(row)


def _decode_obligation(row: dict[str, Any]) -> dict[str, Any]:
    row["asset_codes"] = json.loads(row.pop("asset_codes_json"))
    return row


class LicenseContractRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO license_contracts(contract_code,licensee_name,licensee_code,created_at,updated_at)
               VALUES(?,?,?,?,?)""",
            (data["contract_code"], data["licensee_name"], data["licensee_code"], now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, contract_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM license_contracts WHERE id=?", (contract_id,)).fetchone(),
            "许可合同不存在",
        )

    def by_code(self, contract_code: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM license_contracts WHERE contract_code=?", (contract_code,)
        ).fetchone()
        return dict(row) if row else None

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                "SELECT * FROM license_contracts WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM license_contracts ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def set_status(self, contract_id: int, status: str, reason: str, changed_at: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            """UPDATE license_contracts SET status=?,status_reason=?,status_changed_at=?,
               version=version+1,updated_at=? WHERE id=?""",
            (status, reason, changed_at, now, contract_id),
        )
        return self.get(contract_id)


class LicenseVersionRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, contract_id: int, data: dict[str, Any], version_no: int, created_by: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO license_contract_versions(contract_id,version_no,effective_from,currency,change_reason,created_by,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (contract_id, version_no, data["effective_from"], data.get("currency", "CNY"), data["change_reason"], created_by, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, version_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM license_contract_versions WHERE id=?", (version_id,)).fetchone(),
            "合同版本不存在",
        )

    def list_for_contract(self, contract_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM license_contract_versions WHERE contract_id=? ORDER BY version_no", (contract_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def next_version_no(self, contract_id: int) -> int:
        current = self.connection.execute(
            "SELECT COALESCE(MAX(version_no),0) FROM license_contract_versions WHERE contract_id=?",
            (contract_id,),
        ).fetchone()[0]
        return int(current) + 1

    def applicable_for(self, contract_id: int, window_end: str) -> dict[str, Any] | None:
        """窗口适用的版本：生效日不晚于窗口结束日的最近版本。

        合同修改允许追溯生效，因此以生效日期排序而不是登记顺序；同一
        生效日多次修改时取版本号最大者。已锁定窗口不重新选择版本。
        """
        row = self.connection.execute(
            """SELECT * FROM license_contract_versions
               WHERE contract_id=? AND effective_from<=?
               ORDER BY effective_from DESC,version_no DESC LIMIT 1""",
            (contract_id, window_end),
        ).fetchone()
        return dict(row) if row else None


class LicenseObligationRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create_many(self, version_id: int, obligations: list[dict[str, Any]], now: str) -> list[dict[str, Any]]:
        for item in obligations:
            self.connection.execute(
                """INSERT INTO license_obligations(contract_version_id,territory,product_line,milestone_code,
                       threshold_amount,royalty_rate,asset_codes_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    item["territory"],
                    item["product_line"],
                    item["milestone_code"],
                    item["threshold_amount"],
                    item["royalty_rate"],
                    json.dumps(item["asset_codes"], ensure_ascii=False),
                    now,
                ),
            )
        return self.list_for_version(version_id)

    def list_for_version(self, version_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT * FROM license_obligations WHERE contract_version_id=?
               ORDER BY territory,product_line,milestone_code""",
            (version_id,),
        ).fetchall()
        return [_decode_obligation(dict(row)) for row in rows]

    def get(self, obligation_id: int) -> dict[str, Any]:
        return _decode_obligation(
            _row(
                self.connection.execute("SELECT * FROM license_obligations WHERE id=?", (obligation_id,)).fetchone(),
                "许可义务不存在",
            )
        )


class LicenseWindowRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, contract_id: int, data: dict[str, Any], version_id: int | None, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO license_report_windows(contract_id,period_label,window_start,window_end,due_at,
                   contract_version_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (contract_id, data["period_label"], data["window_start"], data["window_end"], data["due_at"], version_id, now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, window_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM license_report_windows WHERE id=?", (window_id,)).fetchone(),
            "报告窗口不存在",
        )

    def by_label(self, contract_id: int, period_label: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM license_report_windows WHERE contract_id=? AND period_label=?",
            (contract_id, period_label),
        ).fetchone()
        return dict(row) if row else None

    def overlapping(self, contract_id: int, window_start: str, window_end: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM license_report_windows
               WHERE contract_id=? AND status!='void' AND window_start<=? AND window_end>=?
               LIMIT 1""",
            (contract_id, window_end, window_start),
        ).fetchone()
        return dict(row) if row else None

    def list_for_contract(
        self,
        contract_id: int,
        *,
        status: str | None = None,
        payable_status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["contract_id=?"]
        params: list[Any] = [contract_id]
        if status:
            clauses.append("status=?")
            params.append(status)
        if payable_status:
            clauses.append("payable_status=?")
            params.append(payable_status)
        rows = self.connection.execute(
            f"SELECT * FROM license_report_windows WHERE {' AND '.join(clauses)} ORDER BY window_start,id",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_payables(self, contract_id: int | None, payable_status: str | None) -> list[dict[str, Any]]:
        clauses = ["w.status!='void'"]
        params: list[Any] = []
        if contract_id:
            clauses.append("w.contract_id=?")
            params.append(contract_id)
        if payable_status:
            clauses.append("w.payable_status=?")
            params.append(payable_status)
        else:
            clauses.append("w.payable_status IN ('accrued','confirmed')")
        rows = self.connection.execute(
            f"""SELECT w.*,c.contract_code,c.licensee_name
                FROM license_report_windows w JOIN license_contracts c ON c.id=w.contract_id
                WHERE {' AND '.join(clauses)} ORDER BY w.due_at,w.id""",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def restatable(self, contract_id: int) -> list[dict[str, Any]]:
        """可能被追溯版本影响的窗口：未锁定且未作废。"""
        rows = self.connection.execute(
            """SELECT * FROM license_report_windows
               WHERE contract_id=? AND status NOT IN ('locked','void') ORDER BY window_start,id""",
            (contract_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def void_future(self, contract_id: int, effective_from: str, reason: str, now: str) -> list[dict[str, Any]]:
        """作废生效日及之后开始的未结算窗口，已锁定与已支付窗口保持不动。"""
        rows = self.connection.execute(
            """SELECT * FROM license_report_windows
               WHERE contract_id=? AND status NOT IN ('locked','void') AND payable_status!='paid'
                 AND window_start>=?""",
            (contract_id, effective_from),
        ).fetchall()
        voided = []
        for row in rows:
            self.connection.execute(
                """UPDATE license_report_windows SET status='void',payable_status='void',void_reason=?,
                   version=version+1,updated_at=? WHERE id=?""",
                (reason, now, row["id"]),
            )
            voided.append(self.get(row["id"]))
        return voided

    def apply_accrual_result(
        self,
        window_id: int,
        *,
        status: str,
        payable_status: str,
        contract_version_id: int,
        report_id: int,
        reported_amount: float,
        accrued_amount: float,
        adjustment_amount: float,
        payable_amount: float,
        now: str,
    ) -> dict[str, Any]:
        self.connection.execute(
            """UPDATE license_report_windows SET status=?,payable_status=?,contract_version_id=?,report_id=?,
                   reported_amount=?,accrued_amount=?,adjustment_amount=?,payable_amount=?,
                   version=version+1,updated_at=?
               WHERE id=?""",
            (
                status,
                payable_status,
                contract_version_id,
                report_id,
                reported_amount,
                accrued_amount,
                adjustment_amount,
                payable_amount,
                now,
                window_id,
            ),
        )
        return self.get(window_id)

    def refresh_payable(self, window_id: int, adjustment_amount: float, payable_amount: float, now: str) -> dict[str, Any]:
        self.connection.execute(
            """UPDATE license_report_windows SET adjustment_amount=?,payable_amount=?,
               version=version+1,updated_at=? WHERE id=?""",
            (adjustment_amount, payable_amount, now, window_id),
        )
        return self.get(window_id)

    def rebind_version(self, window_id: int, version_id: int, now: str) -> dict[str, Any]:
        self.connection.execute(
            "UPDATE license_report_windows SET contract_version_id=?,version=version+1,updated_at=? WHERE id=?",
            (version_id, now, window_id),
        )
        return self.get(window_id)

    def set_status(self, window_id: int, status: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "UPDATE license_report_windows SET status=?,version=version+1,updated_at=? WHERE id=?",
            (status, now, window_id),
        )
        return self.get(window_id)

    def lock(self, window_id: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """UPDATE license_report_windows SET status='locked',payable_status='confirmed',locked_at=?,
               version=version+1,updated_at=?
               WHERE id=? AND status IN ('reported','reconciling')""",
            (now, now, window_id),
        )
        if cursor.rowcount != 1:
            raise NotFoundError("报告窗口不存在或状态已变化")
        return self.get(window_id)

    def settle(self, window_id: int, paid_at: str, payment_reference: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """UPDATE license_report_windows SET payable_status='paid',paid_at=?,payment_reference=?,
               version=version+1,updated_at=?
               WHERE id=? AND status='locked' AND payable_status='confirmed'""",
            (paid_at, payment_reference, now, window_id),
        )
        if cursor.rowcount != 1:
            raise NotFoundError("报告窗口不存在或不在待支付状态")
        return self.get(window_id)


class LicenseReportRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, window_id: int, contract_id: int, data: dict[str, Any], content_hash: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO license_reports(window_id,contract_id,report_code,idempotency_key,submitted_by,
                   submitted_at,content_hash,note,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                window_id,
                contract_id,
                data["report_code"],
                data["idempotency_key"],
                data["submitted_by"],
                data["submitted_at"],
                content_hash,
                data.get("note", ""),
                now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, report_id: int) -> dict[str, Any]:
        report = _row(
            self.connection.execute("SELECT * FROM license_reports WHERE id=?", (report_id,)).fetchone(),
            "对方报告不存在",
        )
        report["lines"] = self.lines(report_id)
        return report

    def by_idempotency_key(self, window_id: int, idempotency_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM license_reports WHERE window_id=? AND idempotency_key=?",
            (window_id, idempotency_key),
        ).fetchone()
        return dict(row) if row else None

    def add_lines(self, report_id: int, lines: list[dict[str, Any]], now: str) -> None:
        for line in lines:
            self.connection.execute(
                """INSERT INTO license_report_lines(report_id,territory,product_line,milestone_code,reported_sales,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (report_id, line["territory"], line["product_line"], line["milestone_code"], line["reported_sales"], now),
            )

    def lines(self, report_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM license_report_lines WHERE report_id=? ORDER BY territory,product_line,milestone_code",
            (report_id,),
        ).fetchall()
        return [dict(row) for row in rows]


class LicenseAccrualRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def supersede_window(self, window_id: int) -> None:
        self.connection.execute(
            "UPDATE license_accruals SET status='superseded' WHERE window_id=? AND status='active'",
            (window_id,),
        )

    def create(
        self,
        window_id: int,
        obligation: dict[str, Any],
        report_id: int,
        report_line_id: int | None,
        reported_sales: float,
        accrued_amount: float,
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO license_accruals(window_id,obligation_id,contract_version_id,report_id,report_line_id,
                   reported_sales,threshold_amount,royalty_rate,accrued_amount,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                window_id,
                obligation["id"],
                obligation["contract_version_id"],
                report_id,
                report_line_id,
                reported_sales,
                obligation["threshold_amount"],
                obligation["royalty_rate"],
                accrued_amount,
                now,
            ),
        )
        return dict(
            self.connection.execute("SELECT * FROM license_accruals WHERE id=?", (cursor.lastrowid,)).fetchone()
        )

    def active_for_window(self, window_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT a.*,o.territory,o.product_line,o.milestone_code,o.asset_codes_json
               FROM license_accruals a JOIN license_obligations o ON o.id=a.obligation_id
               WHERE a.window_id=? AND a.status='active'
               ORDER BY o.territory,o.product_line,o.milestone_code""",
            (window_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["asset_codes"] = json.loads(item.pop("asset_codes_json"))
            result.append(item)
        return result


class LicenseAdjustmentRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, window_id: int, data: dict[str, Any], created_by: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO license_adjustments(window_id,obligation_id,delta_amount,reason,created_by,created_at)
               VALUES(?,?,?,?,?,?)""",
            (window_id, data.get("obligation_id"), data["delta_amount"], data["reason"], created_by, now),
        )
        return dict(
            self.connection.execute("SELECT * FROM license_adjustments WHERE id=?", (cursor.lastrowid,)).fetchone()
        )

    def list_for_window(self, window_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT a.*,u.display_name AS created_by_name
               FROM license_adjustments a JOIN users u ON u.id=a.created_by
               WHERE a.window_id=? ORDER BY a.id""",
            (window_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def total_for_window(self, window_id: int) -> float:
        return float(
            self.connection.execute(
                "SELECT COALESCE(SUM(delta_amount),0) FROM license_adjustments WHERE window_id=?",
                (window_id,),
            ).fetchone()[0]
        )


class LicenseDifferenceRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(
        self,
        window_id: int,
        report_id: int,
        kind: str,
        *,
        obligation_id: int | None,
        expected: dict[str, Any],
        actual: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO license_differences(window_id,report_id,obligation_id,kind,expected_json,actual_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                window_id,
                report_id,
                obligation_id,
                kind,
                json.dumps(expected, ensure_ascii=False, sort_keys=True),
                json.dumps(actual, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, difference_id: int) -> dict[str, Any]:
        row = _row(
            self.connection.execute("SELECT * FROM license_differences WHERE id=?", (difference_id,)).fetchone(),
            "待复核差异不存在",
        )
        return self._decode(row)

    def pending_for_window(self, window_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM license_differences WHERE window_id=? AND state='pending' ORDER BY id",
            (window_id,),
        ).fetchall()
        return [self._decode(dict(row)) for row in rows]

    def list_for_window(self, window_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM license_differences WHERE window_id=? ORDER BY id",
            (window_id,),
        ).fetchall()
        return [self._decode(dict(row)) for row in rows]

    def resolve(self, difference_id: int, note: str, resolved_by: int | None, now: str) -> dict[str, Any]:
        self.connection.execute(
            """UPDATE license_differences SET state='resolved',resolution_note=?,resolved_by=?,resolved_at=?
               WHERE id=?""",
            (note, resolved_by, now, difference_id),
        )
        return self.get(difference_id)

    def _decode(self, row: dict[str, Any]) -> dict[str, Any]:
        row["expected"] = json.loads(row.pop("expected_json"))
        row["actual"] = json.loads(row.pop("actual_json"))
        return row
