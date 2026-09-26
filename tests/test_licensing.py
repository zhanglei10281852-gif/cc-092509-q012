from __future__ import annotations


OBLIGATIONS = [
    {"territory": "CN", "product_line": "芯片IP", "milestone_code": "M1",
     "threshold_amount": 100000, "royalty_rate": 0.05, "asset_codes": ["PAT-CN-101"]},
    {"territory": "CN", "product_line": "芯片IP", "milestone_code": "M2",
     "threshold_amount": 500000, "royalty_rate": 0.08, "asset_codes": ["PAT-CN-101", "PAT-CN-102"]},
    {"territory": "EU", "product_line": "芯片IP", "milestone_code": "M1",
     "threshold_amount": 200000, "royalty_rate": 0.06, "asset_codes": ["PAT-EP-201"]},
]


def _create_contract(client, admin):
    response = client.post(
        "/api/licensing/contracts",
        headers=admin["headers"],
        json={
            "contract_code": "LIC-2026-001",
            "licensee_name": "华信电子有限公司",
            "licensee_code": "CUST-HX",
            "effective_from": "2026-01-01",
            "obligations": OBLIGATIONS,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_window(client, admin, contract_id, label="2026-Q1", start="2026-01-01", end="2026-03-31"):
    response = client.post(
        f"/api/licensing/contracts/{contract_id}/windows",
        headers=admin["headers"],
        json={"period_label": label, "window_start": start, "window_end": end, "due_at": "2026-04-30"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _report_payload(key="RPT-Q1-KEY", lines=None, code="RPT-Q1"):
    return {
        "report_code": code,
        "idempotency_key": key,
        "submitted_by": "华信电子财务部",
        "submitted_at": "2026-04-25T10:00:00+00:00",
        "lines": lines
        or [
            {"territory": "CN", "product_line": "芯片IP", "milestone_code": "M1", "reported_sales": 300000},
            {"territory": "CN", "product_line": "芯片IP", "milestone_code": "M2", "reported_sales": 600000},
            {"territory": "EU", "product_line": "芯片IP", "milestone_code": "M1", "reported_sales": 150000},
        ],
    }


def _import_report(client, admin, window_id, payload=None):
    response = client.post(
        f"/api/licensing/windows/{window_id}/reports",
        headers=admin["headers"],
        json=payload or _report_payload(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_contract_registration_records_versions_and_obligations(client, admin):
    created = _create_contract(client, admin)
    contract = created["contract"]
    assert contract["status"] == "active"
    assert created["version"]["version_no"] == 1
    assert len(created["obligations"]) == 3
    assert created["obligations"][0]["asset_codes"]

    detail = client.get(f"/api/licensing/contracts/{contract['id']}", headers=admin["headers"])
    assert detail.status_code == 200
    assert detail.json()["versions"][0]["effective_from"] == "2026-01-01"
    duplicate = client.post(
        "/api/licensing/contracts",
        headers=admin["headers"],
        json={
            "contract_code": "LIC-2026-001",
            "licensee_name": "其他公司",
            "licensee_code": "CUST-OTHER",
            "effective_from": "2026-01-01",
            "obligations": OBLIGATIONS,
        },
    )
    assert duplicate.status_code == 409


def test_report_import_computes_accruals_and_payable(client, admin):
    contract = _create_contract(client, admin)["contract"]
    window = _create_window(client, admin, contract["id"])
    result = _import_report(client, admin, window["id"])
    assert result["replayed"] is False
    updated = result["window"]
    assert updated["status"] == "reported"
    assert updated["payable_status"] == "accrued"
    assert updated["reported_amount"] == 1050000
    # (300000-100000)*0.05 + (600000-500000)*0.08 + max(0,150000-200000)*0.06
    assert updated["accrued_amount"] == 18000.0
    assert updated["payable_amount"] == 18000.0
    assert updated["contract_version_id"] == 1

    detail = client.get(f"/api/licensing/windows/{window['id']}", headers=admin["headers"]).json()
    assert len(detail["accruals"]) == 3
    zero_line = next(item for item in detail["accruals"] if item["milestone_code"] == "M1" and item["territory"] == "EU")
    assert zero_line["accrued_amount"] == 0
    assert detail["differences"] == []


def test_reimport_same_report_does_not_double_accrue(client, admin):
    contract = _create_contract(client, admin)["contract"]
    window = _create_window(client, admin, contract["id"])
    first = _import_report(client, admin, window["id"])
    second = client.post(
        f"/api/licensing/windows/{window['id']}/reports",
        headers=admin["headers"],
        json=_report_payload(),
    )
    assert second.status_code == 201
    assert second.json()["replayed"] is True
    assert second.json()["report"]["id"] == first["report"]["id"]
    assert second.json()["window"]["accrued_amount"] == 18000.0

    detail = client.get(f"/api/licensing/windows/{window['id']}", headers=admin["headers"]).json()
    assert len(detail["accruals"]) == 3  # 没有重复计提

    changed = client.post(
        f"/api/licensing/windows/{window['id']}/reports",
        headers=admin["headers"],
        json=_report_payload(lines=[{"territory": "CN", "product_line": "芯片IP", "milestone_code": "M1", "reported_sales": 1}]),
    )
    assert changed.status_code == 409


def test_missing_items_create_pending_differences_and_block_lock(client, admin):
    contract = _create_contract(client, admin)["contract"]
    window = _create_window(client, admin, contract["id"])
    lines = [
        {"territory": "CN", "product_line": "芯片IP", "milestone_code": "M1", "reported_sales": 300000},
        {"territory": "CN", "product_line": "芯片IP", "milestone_code": "M2", "reported_sales": 600000},
    ]
    result = _import_report(client, admin, window["id"], _report_payload(lines=lines))
    assert result["window"]["status"] == "reconciling"

    detail = client.get(f"/api/licensing/windows/{window['id']}", headers=admin["headers"]).json()
    assert len(detail["differences"]) == 1
    difference = detail["differences"][0]
    assert difference["kind"] == "missing_item"
    assert difference["state"] == "pending"
    assert difference["expected"]["territory"] == "EU"

    blocked = client.post(f"/api/licensing/windows/{window['id']}/lock", headers=admin["headers"])
    assert blocked.status_code == 409

    resolved = client.post(
        f"/api/licensing/windows/{window['id']}/differences/{difference['id']}/resolve",
        headers=admin["headers"],
        json={"resolution_note": "对方确认欧洲区本期无销售，下期补报"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["difference"]["state"] == "resolved"
    assert resolved.json()["window"]["status"] == "reported"

    locked = client.post(f"/api/licensing/windows/{window['id']}/lock", headers=admin["headers"])
    assert locked.status_code == 200, locked.text
    assert locked.json()["status"] == "locked"
    assert locked.json()["payable_status"] == "confirmed"
    assert locked.json()["locked_at"]

    # 锁定后不能改变已结算周期
    late_report = client.post(
        f"/api/licensing/windows/{window['id']}/reports",
        headers=admin["headers"],
        json=_report_payload(key="RPT-Q1-REV", code="RPT-Q1-REV"),
    )
    assert late_report.status_code == 409
    late_adjustment = client.post(
        f"/api/licensing/windows/{window['id']}/adjustments",
        headers=admin["headers"],
        json={"delta_amount": 100, "reason": "锁定后不应允许"},
    )
    assert late_adjustment.status_code == 409


def test_unexpected_report_line_is_flagged(client, admin):
    contract = _create_contract(client, admin)["contract"]
    window = _create_window(client, admin, contract["id"])
    lines = _report_payload()["lines"] + [
        {"territory": "US", "product_line": "芯片IP", "milestone_code": "M1", "reported_sales": 50000}
    ]
    result = _import_report(client, admin, window["id"], _report_payload(lines=lines))
    assert result["window"]["status"] == "reconciling"
    detail = client.get(f"/api/licensing/windows/{window['id']}", headers=admin["headers"]).json()
    kinds = {item["kind"] for item in detail["differences"]}
    assert kinds == {"unexpected_item"}
    assert detail["differences"][0]["actual"]["territory"] == "US"


def test_corrected_report_supersedes_accruals_and_clears_differences(client, admin):
    contract = _create_contract(client, admin)["contract"]
    window = _create_window(client, admin, contract["id"])
    lines = [
        {"territory": "CN", "product_line": "芯片IP", "milestone_code": "M1", "reported_sales": 300000},
        {"territory": "CN", "product_line": "芯片IP", "milestone_code": "M2", "reported_sales": 600000},
    ]
    _import_report(client, admin, window["id"], _report_payload(lines=lines))
    corrected = _import_report(
        client,
        admin,
        window["id"],
        _report_payload(key="RPT-Q1-REV", code="RPT-Q1-REV"),
    )
    assert corrected["window"]["status"] == "reported"
    assert corrected["window"]["accrued_amount"] == 18000.0

    detail = client.get(f"/api/licensing/windows/{window['id']}", headers=admin["headers"]).json()
    assert len(detail["accruals"]) == 3  # 旧计提已 supersede，仅保留当前有效三条
    states = {item["state"] for item in detail["differences"]}
    assert states == {"resolved"}
    assert detail["differences"][0]["resolution_note"] == "重新计提后缺项已消除"


def test_retroactive_amendment_restates_open_windows_but_not_locked(client, admin):
    contract = _create_contract(client, admin)["contract"]
    q1 = _create_window(client, admin, contract["id"])
    q2 = _create_window(client, admin, contract["id"], label="2026-Q2", start="2026-04-01", end="2026-06-30")
    _import_report(client, admin, q1["id"])
    q2_lines = [{"territory": "CN", "product_line": "芯片IP", "milestone_code": "M1", "reported_sales": 200000}]
    _import_report(client, admin, q2["id"], _report_payload(lines=q2_lines))
    # Q1 无待复核差异，直接锁定为已结算周期
    locked_q1 = client.post(f"/api/licensing/windows/{q1['id']}/lock", headers=admin["headers"])
    assert locked_q1.status_code == 200

    # 追溯生效的修改：CN/M1 阈值降为 5 万、费率升为 6%，自 2026-01-01 生效
    amended = [dict(item) for item in OBLIGATIONS]
    amended[0] = {**amended[0], "threshold_amount": 50000, "royalty_rate": 0.06}
    version = client.post(
        f"/api/licensing/contracts/{contract['id']}/versions",
        headers=admin["headers"],
        json={"effective_from": "2026-01-01", "change_reason": "补充协议：下调起征点并上调费率", "obligations": amended},
    )
    assert version.status_code == 201, version.text
    assert version.json()["version"]["version_no"] == 2
    restated_ids = [item["id"] for item in version.json()["restated_windows"]]
    assert q2["id"] in restated_ids
    assert q1["id"] not in restated_ids

    q1_after = client.get(f"/api/licensing/windows/{q1['id']}", headers=admin["headers"]).json()["window"]
    q2_after = client.get(f"/api/licensing/windows/{q2['id']}", headers=admin["headers"]).json()["window"]
    assert q1_after["accrued_amount"] == 18000.0  # 已结算周期不变
    assert q1_after["contract_version_id"] == 1
    assert q2_after["accrued_amount"] == 9000.0  # (200000-50000)*0.06
    assert q2_after["contract_version_id"] == version.json()["version"]["id"]


def test_suspension_voids_future_windows_without_rolling_back_history(client, admin):
    contract = _create_contract(client, admin)["contract"]
    q1 = _create_window(client, admin, contract["id"])
    q2 = _create_window(client, admin, contract["id"], label="2026-Q2", start="2026-04-01", end="2026-06-30")
    q3 = _create_window(client, admin, contract["id"], label="2026-Q3", start="2026-07-01", end="2026-09-30")
    _import_report(client, admin, q1["id"])
    client.post(f"/api/licensing/windows/{q1['id']}/lock", headers=admin["headers"])

    suspended = client.post(
        f"/api/licensing/contracts/{contract['id']}/status",
        headers=admin["headers"],
        json={"action": "suspend", "effective_from": "2026-07-01", "reason": "被许可方产线停产整顿"},
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["contract"]["status"] == "suspended"
    voided_ids = [item["id"] for item in suspended.json()["voided_windows"]]
    assert voided_ids == [q3["id"]]

    windows = client.get(f"/api/licensing/contracts/{contract['id']}/windows", headers=admin["headers"]).json()
    by_label = {item["period_label"]: item for item in windows}
    assert by_label["2026-Q1"]["status"] == "locked"  # 历史不回滚
    assert by_label["2026-Q2"]["status"] == "open"  # 生效日前开始的窗口保留
    assert by_label["2026-Q3"]["status"] == "void"
    assert by_label["2026-Q3"]["payable_status"] == "void"
    assert "停产整顿" in by_label["2026-Q3"]["void_reason"]

    rejected = client.post(
        f"/api/licensing/windows/{q3['id']}/reports",
        headers=admin["headers"],
        json=_report_payload(key="RPT-Q3", code="RPT-Q3"),
    )
    assert rejected.status_code == 409

    resumed = client.post(
        f"/api/licensing/contracts/{contract['id']}/status",
        headers=admin["headers"],
        json={"action": "resume", "effective_from": "2026-08-01", "reason": "整改完成恢复履约"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["contract"]["status"] == "active"

    terminated = client.post(
        f"/api/licensing/contracts/{contract['id']}/status",
        headers=admin["headers"],
        json={"action": "terminate", "effective_from": "2026-09-01", "reason": "双方协商提前终止"},
    )
    assert terminated.status_code == 200
    assert terminated.json()["contract"]["status"] == "terminated"
    # 终止后 Q2 窗口（09-01 前开始）保留，但不能再导入报告或登记版本
    blocked_report = client.post(
        f"/api/licensing/windows/{q2['id']}/reports",
        headers=admin["headers"],
        json=_report_payload(key="RPT-Q2", code="RPT-Q2"),
    )
    assert blocked_report.status_code == 409
    blocked_version = client.post(
        f"/api/licensing/contracts/{contract['id']}/versions",
        headers=admin["headers"],
        json={"effective_from": "2026-09-01", "change_reason": "终止后修改", "obligations": OBLIGATIONS},
    )
    assert blocked_version.status_code == 409


def test_adjustment_and_trace_back_to_version_report_and_reasons(client, admin):
    contract = _create_contract(client, admin)["contract"]
    window = _create_window(client, admin, contract["id"])
    _import_report(client, admin, window["id"])

    adjusted = client.post(
        f"/api/licensing/windows/{window['id']}/adjustments",
        headers=admin["headers"],
        json={"delta_amount": -1500.5, "reason": "对方提供季度返利协议，核减计提"},
    )
    assert adjusted.status_code == 201, adjusted.text
    assert adjusted.json()["window"]["payable_amount"] == 16499.5
    assert adjusted.json()["window"]["adjustment_amount"] == -1500.5

    client.post(f"/api/licensing/windows/{window['id']}/lock", headers=admin["headers"])
    payables = client.get(
        "/api/licensing/payables",
        headers=admin["headers"],
        params={"payable_status": "confirmed"},
    )
    assert payables.status_code == 200
    matches = [item for item in payables.json() if item["id"] == window["id"]]
    assert len(matches) == 1
    assert matches[0]["payable_amount"] == 16499.5

    trace = client.get(f"/api/licensing/windows/{window['id']}/trace", headers=admin["headers"])
    assert trace.status_code == 200
    body = trace.json()
    assert body["window"]["payable_amount"] == 16499.5
    assert body["contract"]["contract_code"] == "LIC-2026-001"
    assert body["contract_version"]["version_no"] == 1
    assert body["contract_version"]["effective_from"] == "2026-01-01"
    assert body["report"]["report_code"] == "RPT-Q1"
    assert body["report"]["idempotency_key"] == "RPT-Q1-KEY"
    assert len(body["report"]["lines"]) == 3
    assert len(body["accruals"]) == 3
    assert body["adjustments"][0]["reason"] == "对方提供季度返利协议，核减计提"
    assert body["adjustments"][0]["created_by_name"]

    settled = client.post(
        f"/api/licensing/windows/{window['id']}/settle",
        headers=admin["headers"],
        json={"paid_at": "2026-05-15", "payment_reference": "PAY-2026-0515"},
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["payable_status"] == "paid"
    again = client.post(
        f"/api/licensing/windows/{window['id']}/settle",
        headers=admin["headers"],
        json={"paid_at": "2026-05-16", "payment_reference": "PAY-DUP"},
    )
    assert again.status_code == 409


def test_window_validation_and_permission_checks(client, admin):
    contract = _create_contract(client, admin)["contract"]
    overlap = _create_window(client, admin, contract["id"])
    duplicated = client.post(
        f"/api/licensing/contracts/{contract['id']}/windows",
        headers=admin["headers"],
        json={"period_label": "2026-Q1-REDO", "window_start": "2026-02-01", "window_end": "2026-04-30", "due_at": "2026-05-31"},
    )
    assert duplicated.status_code == 409
    del overlap

    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "readonly.viewer", "name": "只读人员", "permission_codes": ["dossiers.read"]},
    )
    assert role.status_code == 201
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "viewer.one", "password": "Viewer!23456", "display_name": "只读人员甲", "role_codes": ["readonly.viewer"]},
    )
    assert user.status_code == 201
    login = client.post("/api/auth/login", json={"username": "viewer.one", "password": "Viewer!23456", "client_label": "tests"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    denied = client.get("/api/licensing/contracts", headers=headers)
    assert denied.status_code == 403
