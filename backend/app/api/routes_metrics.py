"""指标接口（设计文档 8 / 13 节）。

对外只有固定能力：趋势、版本对比、分群、异常，以及定义与覆盖度查询。
任何 SQL 都是 `MetricService` 的内部实现，接口层不接受 SQL 文本。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.db.session import get_db
from app.metrics.schemas import (
    AnomalyRequest,
    BreakdownRequest,
    TrendRequest,
    VersionCompareRequest,
)
from app.metrics.service import SqlMetricService
from app.models.metrics import MetricDefinition
from app.schemas.metric import (
    MetricAnomalyQuery,
    MetricBreakdownQuery,
    MetricCompareQuery,
    MetricDefinitionOut,
    MetricImportResponse,
    MetricQueryResponse,
    MetricTrendQuery,
)
from app.services.metric_import import MetricImportError, MetricImporter
from app.services.authorization import (
    AuthorizationRequest,
    AuthorizationResource,
    AuthorizationService,
    subject_from_principal,
)

router = APIRouter(prefix="/metrics", tags=["指标服务"])


def _service(session: Session, principal: Principal) -> SqlMetricService:
    """带上访问范围的指标服务：指标级密级判定与缓存键隔离都靠它。"""
    return SqlMetricService(
        session,
        tenant_id=principal.tenant_id,
        roles=list(principal.roles),
        user_id=principal.user.id,
        clearance=principal.user.security_level,
    )


def _require_metric(
    service: SqlMetricService,
    principal: Principal,
    metric_name: str,
    params: dict | None = None,
) -> None:
    """指标级密级：无权访问时返回 403（与知识库越权同一套错误结构），且不泄露数值。

    除了"能不能读这个指标"，还要看"请求的维度值是否在数据范围内"：
    只判前者的话，用户请求 `channel=channel_b`（而他的范围只有 channel_a）会拿到空结果，
    看起来像"这个渠道没数据"——权限问题绝不能被伪装成数据缺失，所以这里直接 403。
    """
    from app.services.metric_access import check_dimension_values

    decision = AuthorizationService(service.session).decide(
        AuthorizationRequest(
            subject=subject_from_principal(principal),
            action="metric:read",
            resource=AuthorizationResource(
                resource_type="metric",
                resource_id=metric_name,
                tenant_id=principal.tenant_id,
            ),
            context={"requested": params or {}},
        )
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "approval_required" if decision.requires_approval else "metric_forbidden",
                "message": decision.reason or f"无权访问指标 {metric_name}",
                "decision": decision.decision,
                "matched_policy_ids": decision.matched_policy_ids,
            },
        )
    scope = service.scope_for(metric_name)
    violation = check_dimension_values(scope, params or {})
    if violation:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "metric_out_of_scope", "message": violation},
        )


@router.get("/definitions", response_model=list[MetricDefinitionOut], summary="指标字典")
def list_definitions(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> list[MetricDefinitionOut]:
    service = _service(session, principal)
    visible = set(service.visible_metric_names([
        row.name
        for row in session.query(MetricDefinition).filter(MetricDefinition.is_active.is_(True)).all()
    ]))
    rows = (
        session.query(MetricDefinition)
        .filter(MetricDefinition.is_active.is_(True))
        .order_by(MetricDefinition.name)
        .all()
    )
    # 无权访问的指标不出现在字典里：清单本身就是信息
    return [
        MetricDefinitionOut.model_validate(item, from_attributes=True)
        for item in rows
        if item.name in visible
    ]


@router.get("/coverage", summary="指标数据覆盖情况（缺失与区间）")
def metric_coverage(
    metric_name: str = Query(...),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    service = _service(session, principal)
    _require_metric(service, principal, metric_name)
    coverage = service.coverage(metric_name)
    coverage["versions"] = service.list_versions(metric_name)
    return coverage


@router.get("/trend", response_model=MetricQueryResponse, summary="指标时间趋势")
def metric_trend(
    query: MetricTrendQuery = Depends(),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> MetricQueryResponse:
    service = _service(session, principal)
    _require_metric(service, principal, query.metric_name, query.model_dump())
    result = service.get_trend(TrendRequest(**query.model_dump()))
    return _to_response(result)


@router.get("/compare", response_model=MetricQueryResponse, summary="版本前后对比")
def metric_compare(
    query: MetricCompareQuery = Depends(),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> MetricQueryResponse:
    service = _service(session, principal)
    _require_metric(service, principal, query.metric_name, query.model_dump())
    result = service.compare_versions(VersionCompareRequest(**query.model_dump()))
    return _to_response(result)


@router.get("/breakdown", response_model=MetricQueryResponse, summary="维度分群")
def metric_breakdown(
    query: MetricBreakdownQuery = Depends(),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> MetricQueryResponse:
    service = _service(session, principal)
    _require_metric(service, principal, query.metric_name, query.model_dump())
    result = service.breakdown(BreakdownRequest(**query.model_dump()))
    return _to_response(result)


@router.get("/anomaly", response_model=MetricQueryResponse, summary="异常点识别")
def metric_anomaly(
    query: MetricAnomalyQuery = Depends(),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> MetricQueryResponse:
    service = _service(session, principal)
    _require_metric(service, principal, query.metric_name, query.model_dump())
    result = service.detect_anomaly(AnomalyRequest(**query.model_dump()))
    return _to_response(result)


@router.post(
    "/import",
    response_model=MetricImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="导入指标数据（CSV / Excel）",
)
def import_metrics(
    file: UploadFile = File(...),
    dataset_version: str | None = Form(default=None),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> MetricImportResponse:
    principal.require("metric:import")
    importer = MetricImporter(session)
    try:
        result = importer.import_bytes(
            tenant_id=principal.tenant_id,
            filename=file.filename or "metrics.csv",
            data=file.file.read(),
            dataset_version=dataset_version,
            user=principal.user,
        )
    except MetricImportError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "metric_import_failed", "message": str(exc)},
        ) from exc
    session.commit()
    return MetricImportResponse(**result)


def _to_response(result) -> MetricQueryResponse:
    payload = result.as_dict()
    return MetricQueryResponse(
        capability=payload["capability"],
        metric_name=payload["metric_name"],
        params=payload["params"],
        rows=payload["rows"],
        summary=payload["summary"],
        dataset_version=payload["dataset_version"],
        source_version=payload["source_version"],
        quality_flags=payload["quality_flags"],
        warnings=payload["warnings"],
        available=payload["available"],
        citations=[
            {
                "source_kind": "metric",
                "metric_ref": {
                    "metric_name": payload["metric_name"],
                    "capability": payload["capability"],
                    "params": payload["params"],
                    "dataset_version": payload["dataset_version"],
                },
            }
        ],
    )
