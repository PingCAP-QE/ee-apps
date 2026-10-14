from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

CI_WEEKLY_COST_SOURCES = (
    ("gcp", "pingcap-testing-account"),
    ("tencent", "100050658403"),
)

_CATEGORY_INFO = {
    "gke": (
        "GKE",
        "Native gke_direct/gke_residual allocation scope; includes idle, system and shared costs.",
    ),
    "gcp_pvc": (
        "Kubernetes PVC / Hyperdisk",
        "Compute Engine disk resource named pvc-*; lifetime/cache ownership is not established.",
    ),
    "artifact_registry": (
        "Artifact Registry",
        "Billing service_name=Artifact Registry; managed registry, not self-hosted Artifactory.",
    ),
    "gcp_bazel": (
        "Bazel buckets",
        "Known pingcap-ci-bazel remote-cache and mirror bucket resource names.",
    ),
    "prow_cache": (
        "Prow job-cache bucket",
        "Cloud Storage resource_name=prow-job-cache; not proven to be Jenkins cache.",
    ),
    "object_storage": (
        "Other object storage",
        "Cloud Storage/COS billing service; bucket not matched to a named cache or registry.",
    ),
    "logging": ("Cloud Logging", "Billing service_name=Cloud Logging."),
    "bigquery": ("BigQuery", "Billing service_name=BigQuery."),
    "networking": (
        "Networking",
        "Networking/NAT/load-balancer/public-IP/GAAP billing services; not an external line invoice.",
    ),
    "openclaw": ("OpenClaw resources", "Compute Engine resource name contains openclaw."),
    "tke": (
        "TKE / Serverless",
        "TKE/Serverless billing services; may include idle capacity and shared services, not only CI jobs.",
    ),
    "cbs": ("CBS disks", "CBS billing service; CI association does not prove dynamic lifetime."),
    "tencent_bazel": (
        "Bazel COS",
        "Known Bazel bucket, or service/project=bazel when bucket identity is missing.",
    ),
    "jenkins_cache": ("Jenkins cache COS", "COS resource_name=ee-jenkins-cache-1453452848."),
    "jenkins_artifacts": (
        "Jenkins artifacts COS",
        "COS resource_name=ee-jenkins-artifacts-1453452848.",
    ),
    "jenkins_storage": (
        "Jenkins COS (cache / artifacts)",
        "COS service/project=jenkins with missing bucket identity; cache/artifacts cannot be split.",
    ),
    "zot": (
        "Zot COS storage",
        "Known Zot bucket or service/project=zot with missing bucket identity; storage only.",
    ),
    "cvm": ("CVM", "Billing service_name=云服务器CVM; includes egress/shared or unidentified VMs."),
    "dns_logs": ("DNS / logs", "Private DNS and CLS billing services."),
    "other": ("Other services", "Remaining billing services/resources; purpose not separately identified."),
}

# First match wins: native GKE costs must not also appear in disk/service groups.
_CATEGORY_CASE = """
CASE
  WHEN c.vendor = 'gcp' THEN
    CASE
      WHEN c.source_allocation_scope IN ('gke_direct', 'gke_residual') THEN 'gke'
      WHEN c.service_name = 'Compute Engine'
        AND (LOWER(c.resource_name) LIKE 'pvc-%'
             OR LOWER(c.resource_name) LIKE '%/disks/pvc-%') THEN 'gcp_pvc'
      WHEN c.service_name = 'Artifact Registry' THEN 'artifact_registry'
      WHEN c.service_name = 'Cloud Storage' AND c.resource_name IN (
        'pingcap-ci-bazel-remote-cache-us-central1',
        'pingcap-ci-bazel-mirror-static-us-central1'
      ) THEN 'gcp_bazel'
      WHEN c.service_name = 'Cloud Storage' AND c.resource_name = 'prow-job-cache' THEN 'prow_cache'
      WHEN c.service_name = 'Cloud Storage' THEN 'object_storage'
      WHEN c.service_name = 'Cloud Logging' THEN 'logging'
      WHEN c.service_name = 'BigQuery' THEN 'bigquery'
      WHEN c.service_name = 'Networking' THEN 'networking'
      WHEN c.service_name = 'Compute Engine'
        AND LOWER(c.resource_name) LIKE '%openclaw%' THEN 'openclaw'
      ELSE 'other'
    END
  WHEN c.vendor = 'tencent' THEN
    CASE
      WHEN c.service_name IN ('容器服务 TKE', 'Serverless 容器服务') THEN 'tke'
      WHEN c.service_name = '云硬盘CBS' THEN 'cbs'
      WHEN c.service_name = 'COS 对象存储'
        AND c.resource_name = 'ee-brc-1453452848' THEN 'tencent_bazel'
      WHEN c.service_name = 'COS 对象存储'
        AND c.resource_name = 'ee-jenkins-cache-1453452848' THEN 'jenkins_cache'
      WHEN c.service_name = 'COS 对象存储'
        AND c.resource_name = 'ee-jenkins-artifacts-1453452848' THEN 'jenkins_artifacts'
      WHEN c.service_name = 'COS 对象存储'
        AND c.resource_name = 'ee-zot-1453452848' THEN 'zot'
      WHEN c.service_name = 'COS 对象存储'
        AND (c.resource_name IS NULL OR c.resource_name = '') THEN
        CASE
          WHEN COALESCE(NULLIF(c.service, ''), c.project) = 'bazel' THEN 'tencent_bazel'
          WHEN COALESCE(NULLIF(c.service, ''), c.project) = 'jenkins' THEN 'jenkins_storage'
          WHEN COALESCE(NULLIF(c.service, ''), c.project) = 'zot' THEN 'zot'
          ELSE 'object_storage'
        END
      WHEN c.service_name = 'COS 对象存储' THEN 'object_storage'
      WHEN c.service_name IN ('NAT网关', '负载均衡', '公网 IP', '全球应用加速GAAP') THEN 'networking'
      WHEN c.service_name = '云服务器CVM' THEN 'cvm'
      WHEN c.service_name IN ('私有域解析 Private DNS', '日志服务CLS') THEN 'dns_logs'
      ELSE 'other'
    END
  ELSE 'other'
END
"""


def get_ci_weekly_cost_category_share(
    connection: Connection,
    start_date: date,
    end_date: date,
    *,
    list_cost_expr: str,
    net_cost_expr: str,
) -> dict[str, Any]:
    """Aggregate fixed-account categories using trusted, USD-normalized SQL expressions."""
    source_where = " OR ".join(
        f"(c.vendor = :vendor_{index} AND c.account_id = :account_{index})"
        for index, _ in enumerate(CI_WEEKLY_COST_SOURCES)
    )
    params: dict[str, Any] = {"start_date": start_date, "end_date": end_date}
    for index, (vendor, account_id) in enumerate(CI_WEEKLY_COST_SOURCES):
        params[f"vendor_{index}"] = vendor
        params[f"account_{index}"] = account_id
    rows = connection.execute(
        text(
            f"""
            SELECT c.vendor, c.account_id, c.usage_date,
                   {_CATEGORY_CASE} AS category_key,
                   SUM(CASE WHEN c.vendor = 'gcp' THEN COALESCE({list_cost_expr}, 0)
                            ELSE COALESCE({net_cost_expr}, 0) END) AS cost
            FROM cost_attribution_daily c
            WHERE ({source_where})
              AND c.usage_date BETWEEN :start_date AND :end_date
            GROUP BY c.vendor, c.account_id, c.usage_date, category_key
            """
        ),
        params,
    ).mappings()
    costs: dict[tuple[str, str], dict[str, Decimal]] = {
        source: {} for source in CI_WEEKLY_COST_SOURCES
    }
    observed_dates: dict[tuple[str, str], set[str]] = {
        source: set() for source in CI_WEEKLY_COST_SOURCES
    }
    for row in rows:
        source = (row["vendor"], row["account_id"])
        key = row["category_key"]
        costs[source][key] = costs[source].get(key, Decimal(0)) + Decimal(str(row["cost"] or 0))
        observed_dates[source].add(str(row["usage_date"]))

    expected_dates = {
        (start_date + timedelta(days=offset)).isoformat()
        for offset in range((end_date - start_date).days + 1)
    }
    accounts = []
    for vendor, account_id in CI_WEEKLY_COST_SOURCES:
        source = (vendor, account_id)
        total = sum(costs[source].values(), Decimal(0))
        accounts.append({
            "cost_source": f"{vendor}:{account_id}",
            "vendor": vendor,
            "cost_basis": "list_cost" if vendor == "gcp" else "net_cost",
            "total_cost": float(total),
            "items": [
                {
                    "key": key,
                    "name": _CATEGORY_INFO[key][0],
                    "value": float(value),
                    "share_pct": float(value / total * 100) if total else None,
                    "evidence": _CATEGORY_INFO[key][1],
                }
                for key, value in sorted(costs[source].items(), key=lambda item: -item[1])
                if value != 0
            ],
            "observed_dates": sorted(observed_dates[source]),
            "missing_dates": sorted(expected_dates - observed_dates[source]),
        })
    return {
        "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        "accounts": accounts,
    }
