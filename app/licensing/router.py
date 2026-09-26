from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.licensing.schemas import (
    AdjustmentCreate,
    ContractCreate,
    ContractStatusChange,
    DiscrepancyResolve,
    ReportCreate,
    VersionCreate,
    WindowCreate,
)
from app.licensing.service import LicenseContractService, LicenseQueryService, LicenseSettlementService

router = APIRouter(prefix="/api/licensing", tags=["许可结算"])


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
def amend_contract(contract_id: int, payload: VersionCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseContractService(connection).amend(principal, contract_id, payload.model_dump())


@router.post("/contracts/{contract_id}/status")
def change_contract_status(contract_id: int, payload: ContractStatusChange, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseContractService(connection).change_status(principal, contract_id, payload.model_dump())


@router.post("/contracts/{contract_id}/windows", status_code=status.HTTP_201_CREATED)
def create_window(contract_id: int, payload: WindowCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).create_window(principal, contract_id, payload.model_dump())


@router.post("/contracts/{contract_id}/reports", status_code=status.HTTP_201_CREATED)
def receive_report(contract_id: int, payload: ReportCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).receive_report(principal, contract_id, payload.model_dump())


@router.get("/contracts/{contract_id}/payables")
def list_payables(
    contract_id: int,
    status: str | None = Query(default=None),
    principal: Principal = Depends(current_principal),
):
    return LicenseQueryService(get_connection()).payables(principal, contract_id, status)


@router.get("/windows/{window_id}")
def window_detail(window_id: int, principal: Principal = Depends(current_principal)):
    return LicenseQueryService(get_connection()).window_detail(principal, window_id)


@router.post("/windows/{window_id}/confirm")
def confirm_window(window_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).confirm_window(principal, window_id)


@router.post("/windows/{window_id}/lock")
def lock_window(window_id: int, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).lock_window(principal, window_id)


@router.post("/discrepancies/{discrepancy_id}/resolve")
def resolve_discrepancy(discrepancy_id: int, payload: DiscrepancyResolve, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).resolve_discrepancy(principal, discrepancy_id, payload.model_dump())


@router.post("/accruals/{accrual_id}/adjustments", status_code=status.HTTP_201_CREATED)
def adjust_accrual(accrual_id: int, payload: AdjustmentCreate, principal: Principal = Depends(current_principal)):
    with transaction(immediate=True) as connection:
        return LicenseSettlementService(connection).adjust_accrual(principal, accrual_id, payload.model_dump())


@router.get("/accruals/{accrual_id}/trace")
def trace_accrual(accrual_id: int, principal: Principal = Depends(current_principal)):
    return LicenseQueryService(get_connection()).trace(principal, accrual_id)
