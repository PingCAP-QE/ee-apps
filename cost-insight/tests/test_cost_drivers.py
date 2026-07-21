import ast
from pathlib import Path

from cost_insight.common.cost_drivers import COST_DRIVER_LABELS, classify_cost_driver


def test_classify_cost_driver_keeps_v1_buckets_small() -> None:
    assert (
        classify_cost_driver(
            {
                "vendor": "aws",
                "service_name": "AmazonEC2",
                "sku_name": "BoxUsage:m7g.large",
                "usage_type": "USE1-BoxUsage:m7g.large",
            }
        )
        == "compute"
    )
    assert (
        classify_cost_driver(
            {
                "vendor": "aws",
                "service_name": "AmazonEC2",
                "sku_name": "SWX7SS3HHS4X3MPP",
                "usage_type": "USE1-NatGateway-Hours",
            }
        )
        == "nat"
    )
    assert (
        classify_cost_driver(
            {
                "vendor": "aws",
                "service_name": "AmazonEC2",
                "sku_name": "SWX7SS3HHS4X3MPP",
                "usage_type": "USE1-DataTransfer-Out-Bytes",
            }
        )
        == "data_transfer"
    )
    assert (
        classify_cost_driver(
            {
                "vendor": "aws",
                "service_name": "AmazonEC2",
                "sku_name": "EBS:VolumeUsage.gp3",
                "usage_type": "USE1-EBS:VolumeUsage.gp3",
            }
        )
        == "block_storage"
    )
    assert (
        classify_cost_driver(
            {"service_name": "Compute Engine", "sku_name": "Storage PD Capacity"}
        )
        == "block_storage"
    )
    assert (
        classify_cost_driver(
            {"service_name": "Compute Engine", "sku_name": "SSD PD Capacity"}
        )
        == "block_storage"
    )
    assert classify_cost_driver({"service_name": "AmazonS3", "sku_name": "TimedStorage-ByteHrs"}) == "object_storage"
    assert classify_cost_driver({"service_name": "Cloud Logging", "sku_name": "Log Storage"}) == "logs"
    assert classify_cost_driver({"service_name": "AmazonEFS", "sku_name": "StorageUsage"}) == "other"
    assert classify_cost_driver({"service_name": "AmazonRDS", "sku_name": "StorageUsage"}) == "other"
    assert classify_cost_driver({"service_name": "AmazonAurora", "sku_name": "StorageUsage"}) == "other"
    assert (
        classify_cost_driver(
            {
                "service_name": "AmazonEC2",
                "sku_name": "InstanceStorage-ByteHrs",
                "usage_type": "USE1-InstanceStorage-ByteHrs",
            }
        )
        == "compute"
    )
    assert (
        classify_cost_driver(
            {
                "service_name": "Compute Engine",
                "sku_name": "Storage optimized instance core running in Americas",
            }
        )
        == "compute"
    )
    assert classify_cost_driver({"service_name": "Mystery", "sku_name": "Custom"}) == "other"


def test_dashboard_cost_driver_labels_cover_classifier_keys() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cost_query_path = repo_root / "ci-dashboard/src/ci_dashboard/api/queries/cost.py"
    module = ast.parse(cost_query_path.read_text())
    dashboard_labels = None
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "COST_DRIVER_LABELS"
            for target in node.targets
        ):
            dashboard_labels = ast.literal_eval(node.value)
            break

    assert dashboard_labels == COST_DRIVER_LABELS
