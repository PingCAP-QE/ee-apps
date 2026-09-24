from cost_insight.common.row_utils import tencent_ci_pool_key


def test_tencent_ci_pool_key_defaults_project_to_service() -> None:
    assert tencent_ci_pool_key(
        {"currency": "cny", "service_name": "COS", "service": "cicd"}
    ) == ("CNY", "COS", "cicd", "cicd")
