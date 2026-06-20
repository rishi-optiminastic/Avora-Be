"""Biometric attendance ingest — the on-prem connector pushes device punches.

HMAC + IP allowlist are enforced by `verify_biometric_webhook` before the handler
(same trust model as the HR webhook). The connector is assumed semi-trusted: the
server still re-derives employee match and in/out from the raw punches.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.deps import BiometricServiceDep, verify_biometric_webhook
from app.schemas.biometric import BiometricIngestResult, BiometricPunchBatch

router = APIRouter(prefix="/attendance/biometric", tags=["biometric"])


@router.post("", response_model=BiometricIngestResult, status_code=status.HTTP_200_OK)
async def ingest_biometric(
    payload: BiometricPunchBatch,
    service: BiometricServiceDep,
    _verified: Annotated[bytes, Depends(verify_biometric_webhook)],
) -> BiometricIngestResult:
    return await service.ingest(payload)
