from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

ALLOCATION_ORIGIN = "cost_insight_derived"
ALLOCATION_METHOD = "proportional_source_split_list_v1"
ALLOCATION_VERSION = "v1"
_CURRENCY_SCALE = Decimal("0.01")
_WEIGHT_SCALE = Decimal("0.0000000000000001")


@dataclass(frozen=True)
class ParentResidualInput:
    usage_date: date
    vendor: str
    account_id: str
    parent_resource_id: str
    parent_direct_list_cost: Decimal
    parent_residual_list_cost: Decimal


@dataclass(frozen=True)
class PodSplitInput:
    pod_resource_id: str
    source_pod_split_list_cost: Decimal
    namespace: str | None = None
    workload_name: str | None = None
    workload_type: str | None = None
    owner: str | None = None
    service: str | None = None
    project: str | None = None
    service_exec_id: str | None = None


@dataclass(frozen=True)
class ParentResidualAllocation:
    parent: ParentResidualInput
    pod: PodSplitInput
    allocation_weight: Decimal
    derived_parent_residual_list_cost: Decimal
    parent_input_hash: str
    allocation_origin: str = ALLOCATION_ORIGIN
    allocation_method: str = ALLOCATION_METHOD
    allocation_version: str = ALLOCATION_VERSION


def allocate_parent_residual_list_cost(
    parent: ParentResidualInput,
    pods: Iterable[PodSplitInput],
) -> tuple[ParentResidualAllocation, ...]:
    participants = sorted(
        (pod for pod in pods if pod.source_pod_split_list_cost > 0),
        key=lambda pod: pod.pod_resource_id,
    )
    if not participants:
        return ()

    denominator = sum((pod.source_pod_split_list_cost for pod in participants), Decimal())
    input_hash = build_parent_input_hash(parent, participants)
    allocations: list[ParentResidualAllocation] = []
    rounded_total = Decimal()
    for pod in participants[:-1]:
        weight = (pod.source_pod_split_list_cost / denominator).quantize(
            _WEIGHT_SCALE,
            rounding=ROUND_HALF_UP,
        )
        allocated = (parent.parent_residual_list_cost * weight).quantize(
            _CURRENCY_SCALE,
            rounding=ROUND_HALF_UP,
        )
        rounded_total += allocated
        allocations.append(
            ParentResidualAllocation(
                parent=parent,
                pod=pod,
                allocation_weight=weight,
                derived_parent_residual_list_cost=allocated,
                parent_input_hash=input_hash,
            )
        )

    final_pod = participants[-1]
    final_weight = (final_pod.source_pod_split_list_cost / denominator).quantize(
        _WEIGHT_SCALE,
        rounding=ROUND_HALF_UP,
    )
    allocations.append(
        ParentResidualAllocation(
            parent=parent,
            pod=final_pod,
            allocation_weight=final_weight,
            derived_parent_residual_list_cost=(
                parent.parent_residual_list_cost - rounded_total
            ).quantize(_CURRENCY_SCALE, rounding=ROUND_HALF_UP),
            parent_input_hash=input_hash,
        )
    )
    return tuple(allocations)


def build_parent_input_hash(
    parent: ParentResidualInput,
    participants: Iterable[PodSplitInput],
) -> str:
    payload = {
        "usage_date": parent.usage_date.isoformat(),
        "vendor": parent.vendor,
        "account_id": parent.account_id,
        "parent_resource_id": parent.parent_resource_id,
        "parent_direct_list_cost": str(parent.parent_direct_list_cost),
        "parent_residual_list_cost": str(parent.parent_residual_list_cost),
        "pods": [
            {
                "pod_resource_id": pod.pod_resource_id,
                "source_pod_split_list_cost": str(pod.source_pod_split_list_cost),
                "namespace": pod.namespace,
                "workload_name": pod.workload_name,
                "workload_type": pod.workload_type,
                "owner": pod.owner,
                "service": pod.service,
                "project": pod.project,
                "service_exec_id": pod.service_exec_id,
            }
            for pod in participants
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
