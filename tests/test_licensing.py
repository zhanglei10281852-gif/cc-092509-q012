from __future__ import annotations


def _create_contract(client, admin, **overrides):
    payload = {
        "contract_code": "LIC-2026-001",
        "licensee_name": "华东医疗器械有限公司",
        "effective_from": "2026-01-01",
        "terms": {
            "currency": "CNY",
            "royalty_rate": 0.1,
            "territories": ["CN", "US"],
            "product_lines": ["implant"],
            "milestone_payments": {"FIRST_SALE": 10000},
        },
        "assets": [{"asset_ref": "ZL-2024-0001"}],
        "change_reason": "初始签订",
    }
    payload.update(overrides)
    response = client.post("/api/licensing/contracts", headers=admin["headers"], json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_window(client, admin, contract_id, **overrides):
    payload = {
        "window_code": "2026-Q1",
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "due_at": "2026-04-30",
        "threshold_amount": 0,
    }
    payload.update(overrides)
    response = client.post(
        f"/api/licensing/contracts/{contract_id}/windows", headers=admin["headers"], json=payload
    )
    assert response.status_code == 201, response.text
    return response.json()


def _receive_report(client, admin, contract_id, window_id, **overrides):
    payload = {
        "report_code": "RPT-Q1",
        "window_id": window_id,
        "lines": [
            {"territory": "CN", "product_line": "implant", "milestone": "FIRST_SALE", "gross_sales": 40000},
            {"territory": "US", "product_line": "implant", "gross_sales": 20000},
        ],
    }
    payload.update(overrides)
    return client.post(f"/api/licensing/contracts/{contract_id}/reports", headers=admin["headers"], json=payload)


def _settle_window(client, admin, contract_id, window_id):
    """导入完整报告、确认并锁定一个窗口。"""
    report = _receive_report(client, admin, contract_id, window_id)
    assert report.status_code == 201, report.text
    confirmed = client.post(f"/api/licensing/windows/{window_id}/confirm", headers=admin["headers"])
    assert confirmed.status_code == 200, confirmed.text
    locked = client.post(f"/api/licensing/windows/{window_id}/lock", headers=admin["headers"])
    assert locked.status_code == 200, locked.text
    return report.json()


def test_full_settlement_flow_with_missing_line(client, admin):
    contract = _create_contract(client, admin)
    contract_id = contract["contract"]["id"]
    assert contract["current_version"]["version_no"] == 1
    assert contract["current_version"]["assets"][0]["asset_ref"] == "ZL-2024-0001"

    window = _create_window(client, admin, contract_id, threshold_amount=5000)
    # 报告只覆盖 CN，缺少 US/implant 维度行
    report = _receive_report(
        client,
        admin,
        contract_id,
        window["id"],
        lines=[{"territory": "CN", "product_line": "implant", "milestone": "FIRST_SALE", "gross_sales": 40000}],
    )
    assert report.status_code == 201, report.text
    body = report.json()
    assert body["replayed"] is False
    amounts = {(item["accrual_kind"], item["territory"]): item["amount"] for item in body["accruals"]}
    assert amounts[("royalty", "CN")] == 4000.0
    assert amounts[("milestone", "CN")] == 10000.0
    assert all(item["status"] == "pending" for item in body["accruals"])
    assert all(item["contract_version_id"] == contract["current_version"]["id"] for item in body["accruals"])
    # 缺项差异：US/implant 未报告；无阈值差异（14000 >= 5000）
    assert [item["discrepancy_type"] for item in body["discrepancies"]] == ["missing_line"]
    discrepancy = body["discrepancies"][0]
    assert (discrepancy["territory"], discrepancy["product_line"]) == ("US", "implant")

    # 存在待复核差异时不能确认
    blocked = client.post(f"/api/licensing/windows/{window['id']}/confirm", headers=admin["headers"])
    assert blocked.status_code == 409

    resolved = client.post(
        f"/api/licensing/discrepancies/{discrepancy['id']}/resolve",
        headers=admin["headers"],
        json={"status": "waived", "note": "被许可方确认美国区本期无销售"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "waived"

    confirmed = client.post(f"/api/licensing/windows/{window['id']}/confirm", headers=admin["headers"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"

    locked = client.post(f"/api/licensing/windows/{window['id']}/lock", headers=admin["headers"])
    assert locked.status_code == 200, locked.text
    assert locked.json()["status"] == "locked"

    payables = client.get(f"/api/licensing/contracts/{contract_id}/payables", headers=admin["headers"])
    assert payables.status_code == 200
    assert {item["status"] for item in payables.json()} == {"locked"}
    assert sum(item["payable_amount"] for item in payables.json()) == 14000.0

    # 从一笔待付金额追溯合同版本、原始报告与人工修正
    accrual_id = payables.json()[0]["id"]
    trace = client.get(f"/api/licensing/accruals/{accrual_id}/trace", headers=admin["headers"])
    assert trace.status_code == 200, trace.text
    detail = trace.json()
    assert detail["contract"]["contract_code"] == "LIC-2026-001"
    assert detail["contract_version"]["version_no"] == 1
    assert detail["contract_version"]["terms"]["royalty_rate"] == 0.1
    assert detail["report"]["report_code"] == "RPT-Q1"
    assert detail["report_line"]["gross_sales"] == 40000
    assert detail["adjustments"] == []


def test_report_reimport_is_idempotent(client, admin):
    contract = _create_contract(client, admin)
    contract_id = contract["contract"]["id"]
    window = _create_window(client, admin, contract_id)
    first = _receive_report(client, admin, contract_id, window["id"])
    assert first.status_code == 201, first.text
    second = _receive_report(client, admin, contract_id, window["id"])
    assert second.status_code == 201, second.text
    assert second.json()["replayed"] is True
    assert second.json()["report"]["id"] == first.json()["report"]["id"]
    assert len(second.json()["accruals"]) == len(first.json()["accruals"])
    # 同一编号但内容不同视为冲突
    changed = _receive_report(
        client,
        admin,
        contract_id,
        window["id"],
        lines=[{"territory": "CN", "product_line": "implant", "gross_sales": 1}],
    )
    assert changed.status_code == 409


def test_report_reimport_after_lock_still_replays_without_double_accrual(client, admin):
    contract = _create_contract(client, admin)
    contract_id = contract["contract"]["id"]
    window = _create_window(client, admin, contract_id)
    report = _receive_report(client, admin, contract_id, window["id"])
    assert report.status_code == 201, report.text
    accrual_count = len(report.json()["accruals"])
    assert client.post(f"/api/licensing/windows/{window['id']}/confirm", headers=admin["headers"]).status_code == 200
    assert client.post(f"/api/licensing/windows/{window['id']}/lock", headers=admin["headers"]).status_code == 200
    replayed = _receive_report(client, admin, contract_id, window["id"])
    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["replayed"] is True
    payables = client.get(f"/api/licensing/contracts/{contract_id}/payables", headers=admin["headers"]).json()
    assert len(payables) == accrual_count
    assert {item["status"] for item in payables} == {"locked"}


def test_duplicate_report_line_and_window_overlap_rejected(client, admin):
    contract = _create_contract(client, admin)
    contract_id = contract["contract"]["id"]
    window = _create_window(client, admin, contract_id)
    overlap = client.post(
        f"/api/licensing/contracts/{contract_id}/windows",
        headers=admin["headers"],
        json={"window_code": "2026-Q1-B", "period_start": "2026-02-01", "period_end": "2026-04-30", "due_at": "2026-05-31"},
    )
    assert overlap.status_code == 409
    duplicated = _receive_report(
        client,
        admin,
        contract_id,
        window["id"],
        lines=[
            {"territory": "CN", "product_line": "implant", "gross_sales": 100},
            {"territory": "CN", "product_line": "implant", "gross_sales": 200},
            {"territory": "US", "product_line": "implant", "gross_sales": 50},
        ],
    )
    assert duplicated.status_code == 422
    assert "重复维度" in duplicated.json()["error"]["message"]


def test_retroactive_amendment_cannot_touch_locked_period(client, admin):
    contract = _create_contract(client, admin)
    contract_id = contract["contract"]["id"]
    q1 = _create_window(client, admin, contract_id)
    _settle_window(client, admin, contract_id, q1["id"])

    # 追溯至已锁定周期内的修改被拒绝
    retroactive = client.post(
        f"/api/licensing/contracts/{contract_id}/versions",
        headers=admin["headers"],
        json={
            "effective_from": "2026-02-01",
            "change_reason": "追溯调整费率",
            "terms": {
                "currency": "CNY",
                "royalty_rate": 0.2,
                "territories": ["CN", "US"],
                "product_lines": ["implant"],
                "milestone_payments": {},
            },
        },
    )
    assert retroactive.status_code == 409
    assert "已经结算" in retroactive.json()["error"]["message"]

    # 生效日晚于已锁定周期末尾的修改被接受，资产默认沿用上一版本
    amended = client.post(
        f"/api/licensing/contracts/{contract_id}/versions",
        headers=admin["headers"],
        json={
            "effective_from": "2026-04-01",
            "change_reason": "第二季度起费率上调",
            "terms": {
                "currency": "CNY",
                "royalty_rate": 0.2,
                "territories": ["CN", "US"],
                "product_lines": ["implant"],
                "milestone_payments": {},
            },
        },
    )
    assert amended.status_code == 201, amended.text
    assert amended.json()["current_version"]["version_no"] == 2
    assert amended.json()["current_version"]["assets"][0]["asset_ref"] == "ZL-2024-0001"

    # 新窗口按新版本费率计提，已锁定周期保持原费率
    q2 = _create_window(
        client, admin, contract_id, window_code="2026-Q2", period_start="2026-04-01", period_end="2026-06-30", due_at="2026-07-31"
    )
    report = _receive_report(
        client,
        admin,
        contract_id,
        q2["id"],
        report_code="RPT-Q2",
        lines=[
            {"territory": "CN", "product_line": "implant", "gross_sales": 10000},
            {"territory": "US", "product_line": "implant", "gross_sales": 5000},
        ],
    )
    assert report.status_code == 201, report.text
    royalty = {item["territory"]: item for item in report.json()["accruals"]}
    assert royalty["CN"]["amount"] == 2000.0
    assert royalty["CN"]["royalty_rate"] == 0.2
    assert royalty["CN"]["contract_version_id"] == amended.json()["current_version"]["id"]
    locked_q1 = client.get(f"/api/licensing/windows/{q1['id']}", headers=admin["headers"]).json()
    assert {item["royalty_rate"] for item in locked_q1["accruals"]} == {0.1, 0.0}


def test_suspend_and_terminate_void_future_windows_without_rollback(client, admin):
    contract = _create_contract(client, admin)
    contract_id = contract["contract"]["id"]
    q1 = _create_window(client, admin, contract_id)
    _settle_window(client, admin, contract_id, q1["id"])
    q2 = _create_window(
        client, admin, contract_id, window_code="2026-Q2", period_start="2026-04-01", period_end="2026-06-30", due_at="2026-07-31"
    )
    q3 = _create_window(
        client, admin, contract_id, window_code="2026-Q3", period_start="2026-07-01", period_end="2026-09-30", due_at="2026-10-31"
    )

    suspended = client.post(
        f"/api/licensing/contracts/{contract_id}/status",
        headers=admin["headers"],
        json={"action": "suspend", "effective_from": "2026-04-01", "reason": "被许可方停产核查"},
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["contract"]["status"] == "suspended"
    assert sorted(suspended.json()["voided_window_ids"]) == sorted([q2["id"], q3["id"]])

    detail = client.get(f"/api/licensing/contracts/{contract_id}", headers=admin["headers"]).json()
    states = {item["window_code"]: item["status"] for item in detail["windows"]}
    assert states == {"2026-Q1": "locked", "2026-Q2": "voided", "2026-Q3": "voided"}

    # 暂停期间不能接收报告，也不能新增窗口
    report = _receive_report(client, admin, contract_id, q2["id"], report_code="RPT-Q2")
    assert report.status_code == 409
    new_window = client.post(
        f"/api/licensing/contracts/{contract_id}/windows",
        headers=admin["headers"],
        json={"window_code": "2026-Q4", "period_start": "2026-10-01", "period_end": "2026-12-31", "due_at": "2027-01-31"},
    )
    assert new_window.status_code == 409

    resumed = client.post(
        f"/api/licensing/contracts/{contract_id}/status",
        headers=admin["headers"],
        json={"action": "resume", "effective_from": "2026-05-01", "reason": "核查通过恢复履约"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["contract"]["status"] == "active"

    terminated = client.post(
        f"/api/licensing/contracts/{contract_id}/status",
        headers=admin["headers"],
        json={"action": "terminate", "effective_from": "2026-06-30", "reason": "双方协商提前终止"},
    )
    assert terminated.status_code == 200, terminated.text
    assert terminated.json()["contract"]["status"] == "terminated"
    assert terminated.json()["contract"]["effective_to"] == "2026-06-30"

    # 终止后历史锁定周期仍在，且不能修改合同
    detail = client.get(f"/api/licensing/contracts/{contract_id}", headers=admin["headers"]).json()
    assert {item["window_code"]: item["status"] for item in detail["windows"]}["2026-Q1"] == "locked"
    amended = client.post(
        f"/api/licensing/contracts/{contract_id}/versions",
        headers=admin["headers"],
        json={
            "effective_from": "2026-07-01",
            "change_reason": "终止后修改",
            "terms": {
                "currency": "CNY",
                "royalty_rate": 0.3,
                "territories": ["CN"],
                "product_lines": ["implant"],
                "milestone_payments": {},
            },
        },
    )
    assert amended.status_code == 409


def test_adjustment_trace_and_lock_guard(client, admin):
    contract = _create_contract(client, admin)
    contract_id = contract["contract"]["id"]
    window = _create_window(client, admin, contract_id)
    report = _receive_report(client, admin, contract_id, window["id"])
    assert report.status_code == 201, report.text
    accrual = next(item for item in report.json()["accruals"] if item["accrual_kind"] == "royalty" and item["territory"] == "CN")

    adjusted = client.post(
        f"/api/licensing/accruals/{accrual['id']}/adjustments",
        headers=admin["headers"],
        json={"delta_amount": -500, "reason": "对方退货折让，核减本期计提"},
    )
    assert adjusted.status_code == 201, adjusted.text

    confirmed = client.post(f"/api/licensing/windows/{window['id']}/confirm", headers=admin["headers"])
    assert confirmed.status_code == 200
    # 已确认计提被修正后窗口退回待确认
    reverted = client.post(
        f"/api/licensing/accruals/{accrual['id']}/adjustments",
        headers=admin["headers"],
        json={"delta_amount": 100, "reason": "汇率尾差补提"},
    )
    assert reverted.status_code == 201, reverted.text
    assert reverted.json()["accrual"]["status"] == "pending"
    assert reverted.json()["window"]["status"] == "reported"

    trace = client.get(f"/api/licensing/accruals/{accrual['id']}/trace", headers=admin["headers"])
    reasons = [item["reason"] for item in trace.json()["adjustments"]]
    assert reasons == ["对方退货折让，核减本期计提", "汇率尾差补提"]
    assert trace.json()["accrual"]["payable_amount"] == 3600.0

    assert client.post(f"/api/licensing/windows/{window['id']}/confirm", headers=admin["headers"]).status_code == 200
    assert client.post(f"/api/licensing/windows/{window['id']}/lock", headers=admin["headers"]).status_code == 200
    locked_adjust = client.post(
        f"/api/licensing/accruals/{accrual['id']}/adjustments",
        headers=admin["headers"],
        json={"delta_amount": 1, "reason": "锁定后试图修正"},
    )
    assert locked_adjust.status_code == 409


def test_threshold_and_out_of_scope_discrepancies(client, admin):
    contract = _create_contract(client, admin)
    contract_id = contract["contract"]["id"]
    window = _create_window(client, admin, contract_id, threshold_amount=100000)
    report = _receive_report(
        client,
        admin,
        contract_id,
        window["id"],
        lines=[
            {"territory": "CN", "product_line": "implant", "gross_sales": 1000},
            {"territory": "US", "product_line": "implant", "gross_sales": 1000},
            {"territory": "EU", "product_line": "implant", "gross_sales": 500},
        ],
    )
    assert report.status_code == 201, report.text
    kinds = sorted(item["discrepancy_type"] for item in report.json()["discrepancies"])
    assert kinds == ["below_threshold", "unexpected_line"]
    # 超范围行仍然计提，等待人工复核
    assert any(item["territory"] == "EU" and item["amount"] == 50.0 for item in report.json()["accruals"])


def test_licensing_permissions_are_enforced(client, admin):
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "lab.reader", "name": "只读研究员", "permission_codes": ["dossiers.read"]},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "reader.one", "password": "Reader!23456", "display_name": "只读用户", "role_codes": ["lab.reader"]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": "reader.one", "password": "Reader!23456", "client_label": "tests"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    contract = _create_contract(client, admin)
    denied_write = client.post(
        "/api/licensing/contracts",
        headers=headers,
        json={
            "contract_code": "LIC-DENIED",
            "licensee_name": "无权用户公司",
            "effective_from": "2026-01-01",
            "terms": {
                "currency": "CNY",
                "royalty_rate": 0.1,
                "territories": ["CN"],
                "product_lines": ["implant"],
                "milestone_payments": {},
            },
            "assets": [{"asset_ref": "ZL-2024-0002"}],
        },
    )
    assert denied_write.status_code == 403
    denied_read = client.get(f"/api/licensing/contracts/{contract['contract']['id']}", headers=headers)
    assert denied_read.status_code == 403
    denied_lock = client.post("/api/licensing/windows/1/lock", headers=headers)
    assert denied_lock.status_code == 403
