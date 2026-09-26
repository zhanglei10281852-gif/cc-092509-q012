from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.licensing.schemas import (
    AdjustmentCreate,
    ContractCreate,
    ContractStatusChange,
    ContractVersionCreate,
    DifferenceResolve,
    ReportImport,
    WindowCreate,
    WindowSettle,
)
from app.licensing.service import (
    LicenseContractService,
    LicenseReportService,
    LicenseSettlementService,
    LicenseWindowService,
)

router = APIRouter(prefix="/api/licensing", tags=["许可义务与结算"])


@router.post("/contracts", status_code=status.HTTP_201_CREATED)
def create_contract(payload: ContractCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseContractService(connection).create(principal, payload.model_dump())


@router.get("/contracts")
def list_contracts(status: str | None = Query(default=None), principal: Principal = Depends(current_principal)):
    return LicenseContractService(get_connection()).list(principal, status)


@router.get("/contracts/{contract_id}")
def contract_detail(contract_id: int, principal: Principal = Depends(current_principal)):
    return LicenseContractService(get_connection()).detail(principal, contract_id)


@router.post("/contracts/{contract_id}/versions", status_code=status.HTTP_201_CREATED)
def register_version(contract_id: int, payload: ContractVersionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseContractService(connection).register_version(principal, contract_id, payload.model_dump())


@router.post("/contracts/{contract_id}/status")
def change_status(contract_id: int, payload: ContractStatusChange, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseContractService(connection).change_status(principal, contract_id, payload.model_dump())


@router.post("/contracts/{contract_id}/windows", status_code=status.HTTP_201_CREATED)
def create_window(contract_id: int, payload: WindowCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseWindowService(connection).create(principal, contract_id, payload.model_dump())


@router.get("/contracts/{contract_id}/windows")
def list_windows(
    contract_id: int,
    status: str | None = Query(default=None),
    payable_status: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return LicenseWindowService(get_connection()).list_for_contract(principal, contract_id, status, payable_status)


@router.get("/payables")
def list_payables(
    contract_id: int | None = Query(default=None),
    payable_status: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return LicenseWindowService(get_connection()).payables(principal, contract_id, payable_status)


@router.get("/windows/{window_id}")
def window_detail(window_id: int, principal: Principal = Depends(current_principal)):
    return LicenseWindowService(get_connection()).detail(principal, window_id)


@router.post("/windows/{window_id}/reports", status_code=status.HTTP_201_CREATED)
def import_report(window_id: int, payload: ReportImport, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseReportService(connection).import_report(principal, window_id, payload.model_dump())


@router.post("/windows/{window_id}/adjustments", status_code=status.HTTP_201_CREATED)
def create_adjustment(window_id: int, payload: AdjustmentCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).adjust(principal, window_id, payload.model_dump())


@router.post("/windows/{window_id}/differences/{difference_id}/resolve")
def resolve_difference(
    window_id: int,
    difference_id: int,
    payload: DifferenceResolve,
    principal: Principal = Depends(current_principal),
):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).resolve_difference(
            principal, window_id, difference_id, payload.model_dump()
        )


@router.post("/windows/{window_id}/lock")
def lock_window(window_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).lock(principal, window_id)


@router.post("/windows/{window_id}/settle")
def settle_window(window_id: int, payload: WindowSettle, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).settle(principal, window_id, payload.model_dump())


@router.get("/windows/{window_id}/trace")
def trace_window(window_id: int, principal: Principal = Depends(current_principal)):
    return LicenseSettlementService(get_connection()).trace(principal, window_id)
