#!/usr/bin/env python3
"""Render suspended opt-in CronJobs; never contacts Kubernetes or runs a job."""

import argparse
import json
import re


def render(namespace, image, db_secret, credential_secret, profile_configmap):
    for value in (namespace, db_secret, credential_secret, profile_configmap):
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", value):
            raise ValueError("Explicit valid Kubernetes names are required")
    if not image or any(char.isspace() for char in image):
        raise ValueError("Explicit image reference is required")
    items = []
    for name, schedule, command, cloud in (
        ("maintenance", "*/15 * * * *", "utilization-maintain", False),
        ("inventory", "*/30 * * * *", "sync-tencent-utilization-inventory", True),
        ("daily", "30 0,6,12,18 * * *", "sync-tencent-utilization", True),
    ):
        container = {
            "name": "job",
            "image": image,
            "args": [command],
            "envFrom": [{"secretRef": {"name": db_secret}}],
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "2", "memory": "2Gi"},
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
        }
        pod = {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000},
            "containers": [container],
        }
        if cloud:
            container["args"].extend(["--profile", "/etc/utilization/profile.json"])
            container["envFrom"].append({"secretRef": {"name": credential_secret}})
            container["volumeMounts"] = [
                {"name": "profile", "mountPath": "/etc/utilization", "readOnly": True}
            ]
            pod["volumes"] = [{"name": "profile", "configMap": {"name": profile_configmap}}]
        items.append(
            {
                "apiVersion": "batch/v1",
                "kind": "CronJob",
                "metadata": {"name": "tencent-utilization-" + name, "namespace": namespace},
                "spec": {
                    "suspend": True,
                    "schedule": schedule,
                    "timeZone": "Asia/Shanghai",
                    "concurrencyPolicy": "Forbid",
                    "startingDeadlineSeconds": 600,
                    "successfulJobsHistoryLimit": 1,
                    "failedJobsHistoryLimit": 2,
                    "jobTemplate": {
                        "spec": {
                            "backoffLimit": 0,
                            "ttlSecondsAfterFinished": 86400,
                            "activeDeadlineSeconds": 600 if name == "maintenance" else 1200,
                            "template": {"spec": pod},
                        }
                    },
                },
            }
        )
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("namespace", "image", "db-secret", "credential-secret", "profile-configmap"):
        parser.add_argument("--" + name, required=True)
    print(json.dumps(render(**vars(parser.parse_args())), indent=2))


if __name__ == "__main__":
    main()
