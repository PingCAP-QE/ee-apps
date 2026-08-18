from datetime import date
from decimal import Decimal

from cost_insight.jobs.parent_residual_allocation import (
    ParentResidualInput,
    PodSplitInput,
    allocate_parent_residual_list_cost,
)


def _parent(residual: str = "10.00000000") -> ParentResidualInput:
    return ParentResidualInput(
        usage_date=date(2026, 8, 2),
        vendor="aws",
        account_id="946646677266",
        parent_resource_id="i-0ef88ef97606efb63",
        parent_direct_list_cost=Decimal("20.00000000"),
        parent_residual_list_cost=Decimal(residual),
    )


def test_parent_residual_allocation_uses_positive_source_split_weights() -> None:
    allocations = allocate_parent_residual_list_cost(
        _parent(),
        (
            PodSplitInput("pod-b", Decimal("3")),
            PodSplitInput("pod-a", Decimal("1")),
            PodSplitInput("tiworkload-agent", Decimal("0")),
        ),
    )

    assert [allocation.pod.pod_resource_id for allocation in allocations] == ["pod-a", "pod-b"]
    assert [allocation.derived_parent_residual_list_cost for allocation in allocations] == [
        Decimal("2.50000000"),
        Decimal("7.50000000"),
    ]
    assert sum(
        (allocation.derived_parent_residual_list_cost for allocation in allocations),
        Decimal(),
    ) == Decimal("10.00000000")


def test_parent_residual_allocation_keeps_rounding_remainder_deterministic() -> None:
    allocations = allocate_parent_residual_list_cost(
        _parent("1.00000000"),
        (
            PodSplitInput("pod-z", Decimal("1")),
            PodSplitInput("pod-a", Decimal("1")),
            PodSplitInput("pod-m", Decimal("1")),
        ),
    )

    assert [allocation.derived_parent_residual_list_cost for allocation in allocations] == [
        Decimal("0.33333333"),
        Decimal("0.33333333"),
        Decimal("0.33333334"),
    ]
    assert allocations[0].parent_input_hash == allocations[-1].parent_input_hash


def test_parent_residual_allocation_quantizes_persisted_weights() -> None:
    allocations = allocate_parent_residual_list_cost(
        _parent(),
        (PodSplitInput("pod-a", Decimal("1")), PodSplitInput("pod-b", Decimal("2"))),
    )

    assert [allocation.allocation_weight for allocation in allocations] == [
        Decimal("0.333333333333333333333333"),
        Decimal("0.666666666666666666666667"),
    ]


def test_parent_residual_allocation_quantizes_final_remainder() -> None:
    allocations = allocate_parent_residual_list_cost(
        _parent("1.000000009"),
        (PodSplitInput("pod-a", Decimal("1")), PodSplitInput("pod-b", Decimal("1"))),
    )

    assert [allocation.derived_parent_residual_list_cost for allocation in allocations] == [
        Decimal("0.50000000"),
        Decimal("0.50000001"),
    ]


def test_parent_residual_allocation_leaves_zero_denominator_unallocated() -> None:
    allocations = allocate_parent_residual_list_cost(
        _parent(),
        (PodSplitInput("tiworkload-agent", Decimal("0")),),
    )

    assert allocations == ()
