"""Offline, rendered-text contracts: no YAML parser, credentials or cloud calls."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


PROJECT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "pod-watcher"
DEPLOYMENT = "render_pod_watcher_deployment.sh"
RBAC = "render_pod_watcher_rbac.sh"
GCP_SOURCE = "pingcap-testing-account"
TENCENT_SOURCE = "tencent:ap-beijing:cls-9zpon9y1"
TENCENT_NAMESPACES = (
    "prow-test-pods,jenkins-agents,jenkins-pd,jenkins-ticdc,jenkins-tidb,"
    "jenkins-tiflash,jenkins-tiflow,jenkins-tikv"
)
TENCENT_CONTEXT = [
    "--cloud-phase", "TENCENT", "--cluster-name", "pingcap-ee", "--location", "ap-beijing",
]


def render(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PROJECT / "scripts" / script), *args],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )


def successful_render(script: str, *args: str) -> str:
    result = render(script, *args)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout


def assert_rejected(script: str, *args: str) -> None:
    result = render(script, *args)
    assert result.returncode != 0
    assert result.stdout == "", "invalid input must not leave a partial manifest"
    assert result.stderr.strip()


def rendered_env(text: str) -> dict[str, str]:
    # The renderer deliberately uses the JSON-compatible YAML scalar subset.
    # Decode only individual scalars; do not introduce a YAML parser dependency.
    env_block = text.split("          env:\n", 1)[1].split("          startupProbe:", 1)[0]
    lines = env_block.splitlines()
    env = {}
    for index, line in enumerate(lines):
        if line.startswith("            - name: "):
            value = lines[index + 1].strip().removeprefix("value: ")
            assert value.startswith('"') and value.endswith('"')
            env[line.strip().removeprefix("- name: ")] = json.loads(value)
    return env


@pytest.mark.parametrize(
    "args",
    [
        ["--gcp-project", GCP_SOURCE],
        ["--source-project", GCP_SOURCE],
        ["--source-project", GCP_SOURCE, "--cloud-phase", "GCP"],
        ["--source-project", GCP_SOURCE, "--gcp-project", GCP_SOURCE],
        ["--gcp-project", GCP_SOURCE, "--source-project", GCP_SOURCE],
    ],
    ids=["legacy", "generic-default", "generic-explicit", "equal-aliases", "equal-reversed"],
)
def test_gcp_deployment_fixture(args: list[str]) -> None:
    text = successful_render(DEPLOYMENT, *args)
    assert text == (FIXTURES / "gcp.yaml").read_text()
    env = rendered_env(text)
    assert env["CI_DASHBOARD_GCP_PROJECT"] == GCP_SOURCE
    assert env["CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT"] == GCP_SOURCE
    assert env["CI_DASHBOARD_POD_WATCH_CLOUD_PHASE"] == "GCP"


def test_generic_tencent_deployment_fixture() -> None:
    text = successful_render(
        DEPLOYMENT,
        "--source-project", TENCENT_SOURCE, *TENCENT_CONTEXT,
        "--namespace", "ee-cd",
        "--deployment-name", "ci-dashboard-pod-watcher-tencent",
        "--service-account", "ci-dashboard-pod-watcher",
        "--ca-secret", "ci-dashboard-ca",
        "--pod-event-namespaces", TENCENT_NAMESPACES,
    )
    assert text == (FIXTURES / "tencent.yaml").read_text()
    env = rendered_env(text)
    assert "CI_DASHBOARD_GCP_PROJECT" not in env
    assert env["CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT"] == TENCENT_SOURCE
    assert env["CI_DASHBOARD_POD_WATCH_CLOUD_PHASE"] == "TENCENT"
    assert env["CI_DASHBOARD_KUBERNETES_CLUSTER_NAME"] == "pingcap-ee"
    assert env["CI_DASHBOARD_KUBERNETES_LOCATION"] == "ap-beijing"
    assert "jenkins" not in env["CI_DASHBOARD_POD_EVENT_NAMESPACES"].split(",")
    assert 'args: ["watch-pods"]' in text
    assert "sync-pods" not in text
    assert "  replicas: 1\n  strategy:\n    type: Recreate\n" in text
    assert "RollingUpdate" not in text
    assert "kind: Service\n" not in text


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--source-project", ""],
        ["--gcp-project", ""],
        ["--source-project", " leading"],
        ["--source-project", "trailing "],
        ["--source-project", "\u00a0legacy"],
        ["--source-project", "legacy\u2003"],
        ["--source-project", "\u0085legacy"],
        ["--source-project", "legacy\u0085"],
        ["--source-project", "legacy\u2007"],
        ["--source-project", "\u202flegacy"],
        ["--source-project", "bad\nsource"],
        ["--source-project", "bad\tsource"],
        ["--source-project", "bad\x01source"],
        ["--source-project", "p" * 129],
        ["--source-project", "中文" * 65],
        ["--source-project", GCP_SOURCE, "--gcp-project", "other"],
        ["--gcp-project", "other", "--source-project", GCP_SOURCE],
        ["--source-project", "", "--gcp-project", GCP_SOURCE],
        ["--source-project", GCP_SOURCE, "--cloud-phase", "AWS"],
        ["--source-project", GCP_SOURCE, "--cloud-phase", "gcp"],
        ["--source-project", GCP_SOURCE, *TENCENT_CONTEXT],
        ["--source-project", TENCENT_SOURCE],
        ["--gcp-project", TENCENT_SOURCE, *TENCENT_CONTEXT],
        ["--source-project", TENCENT_SOURCE, "--gcp-project", TENCENT_SOURCE,
         *TENCENT_CONTEXT],
        ["--source-project", "Tencent:ap-beijing:cls-9zpon9y1", *TENCENT_CONTEXT],
        ["--source-project", "tencent:AP-BEIJING:cls-9zpon9y1", *TENCENT_CONTEXT],
        ["--source-project", "tencent:beijing:cls-9zpon9y1", *TENCENT_CONTEXT],
        ["--source-project", "tencent:ap-beijing:CLS-9zpon9y1", *TENCENT_CONTEXT],
        ["--source-project", "tencent:ap-beijing:cls-", *TENCENT_CONTEXT],
        ["--source-project", "tencent:ap-beijing:cls-abc/def", *TENCENT_CONTEXT],
        ["--source-project", "tencent:ap-beijing:cls-" + "a" * 28, *TENCENT_CONTEXT],
        ["--source-project", TENCENT_SOURCE, "--cloud-phase", "TENCENT"],
        ["--source-project", TENCENT_SOURCE, *TENCENT_CONTEXT, "--cluster-name", " "],
        ["--source-project", TENCENT_SOURCE, *TENCENT_CONTEXT, "--location", ""],
        ["--source-project", TENCENT_SOURCE, *TENCENT_CONTEXT, "--location", "ap-shanghai"],
    ],
)
def test_invalid_identity_and_context_emit_nothing(args: list[str]) -> None:
    assert_rejected(DEPLOYMENT, *args)


@pytest.mark.parametrize("cluster", [" ", "\x1c", "\x1d", "\x1e", "\x1f", "\u0085", "\u00a0", "\u2007", "\u202f"])
def test_tencent_cluster_requires_nonblank_display_name(cluster: str) -> None:
    assert_rejected(
        DEPLOYMENT, "--source-project", TENCENT_SOURCE, *TENCENT_CONTEXT,
        "--cluster-name", cluster,
    )


@pytest.mark.parametrize("source", ["p" * 128, "中文" * 64, "legacy:project", "legacy\x7fproject"])
def test_legacy_source_boundaries_remain_accepted(source: str) -> None:
    text = successful_render(DEPLOYMENT, "--source-project", source)
    assert rendered_env(text)["CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT"] == source


def test_tencent_state_key_accepts_exactly_64_characters() -> None:
    source = "tencent:ap-beijing:cls-" + "a" * 27
    assert len(f"ci-watch-pods:{source}") == 64
    text = successful_render(DEPLOYMENT, "--source-project", source, *TENCENT_CONTEXT)
    assert rendered_env(text)["CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT"] == source
    assert_rejected(DEPLOYMENT, "--source-project", source + "a", *TENCENT_CONTEXT)


@pytest.mark.parametrize("replicas", ["0", "2", "-1", "01", "1.0", "1\nkind: Secret"])
def test_single_writer_policy(replicas: str) -> None:
    assert_rejected(DEPLOYMENT, "--gcp-project", GCP_SOURCE, "--replicas", replicas)


@pytest.mark.parametrize(
    "option",
    [
        "--watch-timeout-seconds", "--retry-delay-seconds", "--health-port",
        "--stale-after-seconds", "--db-batch-size", "--db-retry-attempts",
        "--db-retry-base-delay-ms", "--db-retry-max-delay-ms",
    ],
)
@pytest.mark.parametrize("value", ["0", "1\nkind: Secret"])
def test_invalid_numeric_controls_emit_nothing(option: str, value: str) -> None:
    assert_rejected(DEPLOYMENT, "--gcp-project", GCP_SOURCE, option, value)


@pytest.mark.parametrize(
    "args",
    [
        ["--health-port", "65536"],
        ["--health-port", "999999999999999999999999999999999"],
        ["--jenkins-prefix-cache-seconds", "-1"],
        ["--image-pull-policy", "sometimes"],
        ["--namespace", ""],
        ["--image", ""],
        ["--deployment-name", " "],
        ["--db-secret", ""],
        ["--memory-limit", ""],
        ["--ca-secret", "ca", "--ca-key", "../ca.crt"],
        ["--ca-secret", "ca", "--ca-key", ""],
    ],
)
def test_invalid_deployment_options_emit_nothing(args: list[str]) -> None:
    assert_rejected(DEPLOYMENT, "--gcp-project", GCP_SOURCE, *args)


@pytest.mark.parametrize(
    "value",
    [
        "true", "null", "001", "[a,b]", "a: b # comment", "O'Reilly: \"quoted\" \\dir",
        "line\nkind: Secret\r\ttab", "\x01\x7f\x80\x85\x9f", "中文\u2028line\u2029end", "\ufffe\uffff",
    ],
)
def test_scalar_escaping_preserves_values_without_injecting_yaml(value: str) -> None:
    text = successful_render(
        DEPLOYMENT, "--gcp-project", GCP_SOURCE, "--cluster-name", value,
    )
    assert rendered_env(text)["CI_DASHBOARD_KUBERNETES_CLUSTER_NAME"] == value
    assert text.count("\nkind:") == 1
    assert "\nkind: Secret" not in text


def test_source_and_non_env_scalars_are_quoted() -> None:
    source = "O'Reilly: \"quoted\" # true \\dir"
    text = successful_render(
        DEPLOYMENT, "--source-project", source, "--namespace", "true",
        "--deployment-name", "null", "--db-secret", "false", "--service-account", "yes",
        "--log-level", "INFO", "--location", "001",
    )
    assert rendered_env(text)["CI_DASHBOARD_POD_WATCH_SOURCE_PROJECT"] == source
    assert rendered_env(text)["CI_DASHBOARD_GCP_PROJECT"] == source
    assert rendered_env(text)["CI_DASHBOARD_KUBERNETES_LOCATION"] == "001"
    assert '  namespace: "true"\n' in text
    assert '  name: "null"\n' in text
    assert '                name: "false"\n' in text
    assert '      serviceAccountName: "yes"\n' in text


def test_existing_deployment_tuning_controls() -> None:
    text = successful_render(
        DEPLOYMENT, "--gcp-project", GCP_SOURCE,
        "--replicas", "1",
        "--image", "example.invalid/ci-dashboard-jobs:offline", "--image-pull-policy", "Never",
        "--ca-secret", "db-ca", "--ca-key", "root.pem",
        "--watch-timeout-seconds", "60", "--retry-delay-seconds", "2",
        "--health-port", "8099", "--stale-after-seconds", "180",
        "--jenkins-prefix-cache-seconds", "0", "--db-batch-size", "7",
        "--db-retry-attempts", "4", "--db-retry-base-delay-ms", "50",
        "--db-retry-max-delay-ms", "100", "--cpu-request", "250m",
        "--memory-request", "512Mi", "--cpu-limit", "2", "--memory-limit", "4Gi",
    )
    env = rendered_env(text)
    assert env["TIDB_SSL_CA"] == "/var/run/ci-dashboard/ssl/root.pem"
    for name, expected in {
        "TIMEOUT_SECONDS": "60", "RETRY_DELAY_SECONDS": "2", "HEALTH_PORT": "8099",
        "STALE_AFTER_SECONDS": "180", "DB_BATCH_SIZE": "7", "DB_RETRY_ATTEMPTS": "4",
        "DB_RETRY_BASE_DELAY_MS": "50", "DB_RETRY_MAX_DELAY_MS": "100",
    }.items():
        assert env[f"CI_DASHBOARD_POD_WATCH_{name}"] == expected
    assert env["CI_DASHBOARD_JENKINS_POD_NAME_PREFIX_CACHE_SECONDS"] == "0"
    for snippet in [
        'image: "example.invalid/ci-dashboard-jobs:offline"', 'imagePullPolicy: "Never"',
        "containerPort: 8099", 'secretName: "db-ca"', 'key: "root.pem"', 'path: "root.pem"',
        'cpu: "250m"', 'memory: "512Mi"', 'cpu: "2"', 'memory: "4Gi"',
    ]:
        assert snippet in text


def test_minimal_rbac_fixture() -> None:
    text = successful_render(RBAC)
    assert text == (FIXTURES / "rbac.yaml").read_text()
    documents = text.split("---\n")
    assert len(documents) == 4
    assert 'resources: ["pods", "events"]' in documents[0]
    assert 'verbs: ["get", "list", "watch"]' in documents[0]
    assert 'apiGroups: [""]' in documents[0]
    assert all("\nkind: RoleBinding\n" in binding for binding in documents[1:])
    for forbidden in [
        "ClusterRoleBinding", "persistentvolumeclaims", "nodes", "secrets", "pods/log",
        "pods/exec", '"create"', '"update"', '"patch"', '"delete"', '"*"',
    ]:
        assert forbidden not in text


def test_tencent_rbac_targets_only_requested_namespaces() -> None:
    text = successful_render(
        RBAC, "--name", "tencent-watcher", "--service-account-namespace", "ee-cd",
        "--service-account-name", "watcher", "--target-namespaces", TENCENT_NAMESPACES,
    )
    bindings = text.split("---\n")[1:]
    assert len(bindings) == 8
    for namespace, binding in zip(TENCENT_NAMESPACES.split(","), bindings, strict=True):
        assert f'  namespace: "{namespace}"\n' in binding
        assert '    name: "watcher"\n    namespace: "ee-cd"\n' in binding
        assert '  kind: ClusterRole\n  name: "tencent-watcher"\n' in binding
    assert '  namespace: "jenkins"\n' not in text
    assert "ClusterRoleBinding" not in text


def test_rbac_whitespace_and_scalar_quoting() -> None:
    text = successful_render(
        RBAC, "--name", 'a: "b" # c \\d\nkind: Secret',
        "--service-account-name", "true", "--service-account-namespace", "false",
        "--target-namespaces", " prow-test-pods , jenkins-tidb ",
    )
    assert '  namespace: "prow-test-pods"\n' in text
    assert '  namespace: "jenkins-tidb"\n' in text
    assert '    name: "true"\n    namespace: "false"\n' in text
    assert 'name: "a: \\"b\\" # c \\\\d\\u000akind: Secret"' in text
    assert "\nkind: Secret" not in text


@pytest.mark.parametrize("namespaces", ["", " ", ",a", "a,", "a,,b", "a, ,b", "a,INVALID", "a,b\nc"])
@pytest.mark.parametrize("script,option", [(DEPLOYMENT, "--pod-event-namespaces"), (RBAC, "--target-namespaces")])
def test_bad_namespace_lists_never_emit_partial_output(
    script: str, option: str, namespaces: str,
) -> None:
    args = ["--source-project", GCP_SOURCE] if script == DEPLOYMENT else []
    assert_rejected(script, *args, option, namespaces)


@pytest.mark.parametrize("option", ["--name", "--service-account-name", "--service-account-namespace"])
def test_blank_rbac_identity_is_rejected(option: str) -> None:
    assert_rejected(RBAC, option, " ")


@pytest.mark.parametrize("script", [DEPLOYMENT, RBAC])
def test_plain_bash_needs_no_python_or_yaml_executable(script: str, tmp_path: Path) -> None:
    # Supply only the renderer's existing core utility, not Python, jq or yq.
    (tmp_path / "cat").symlink_to("/bin/cat")
    args = ["--gcp-project", GCP_SOURCE] if script == DEPLOYMENT else []
    result = subprocess.run(
        ["/bin/bash", str(PROJECT / "scripts" / script), *args],
        env={"PATH": str(tmp_path), "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    fixture = "gcp.yaml" if script == DEPLOYMENT else "rbac.yaml"
    assert result.stdout == (FIXTURES / fixture).read_text()


@pytest.mark.parametrize("script", [DEPLOYMENT, RBAC])
def test_help_and_argument_errors(script: str) -> None:
    help_result = render(script, "--help")
    assert help_result.returncode == 0
    assert "Usage:" in help_result.stdout
    assert "apiVersion:" not in help_result.stdout
    assert_rejected(script, "--unknown", "value")
    assert_rejected(script, "--namespace" if script == DEPLOYMENT else "--name")
    assert_rejected(script, "--namespace" if script == DEPLOYMENT else "--name", "--help")
