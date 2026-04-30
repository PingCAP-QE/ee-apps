from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping


def _read_required(environ: Mapping[str, str], key: str) -> str:
    value = environ.get(key)
    if value is None or value.strip() == "":
        raise ValueError(f"Missing required environment variable: {key}")
    return value


def _read_int(environ: Mapping[str, str], key: str, default: int) -> int:
    raw = environ.get(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{key} must be positive, got {raw!r}")
    return value


def _read_csv(environ: Mapping[str, str], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = environ.get(key)
    if raw is None:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


@dataclass(frozen=True)
class DatabaseSettings:
    url: str | None
    host: str | None
    port: int | None
    user: str | None
    password: str | None
    database: str | None
    ssl_ca: str | None


@dataclass(frozen=True)
class JobSettings:
    batch_size: int = 1000
    refresh_group_batch_size: int = 25
    refresh_build_limit: int = 5000


@dataclass(frozen=True)
class KafkaSettings:
    bootstrap_servers: tuple[str, ...] = ()
    jenkins_events_topic: str = "jenkins-event"
    jenkins_consumer_group: str = "ci-dashboard-v3-jenkins-worker"
    poll_timeout_ms: int = 1000


@dataclass(frozen=True)
class JenkinsSettings:
    internal_base_url: str | None = None
    username: str | None = None
    api_token: str | None = None
    http_timeout_seconds: int = 30
    progressive_probe_start: int = 2147483647


@dataclass(frozen=True)
class JenkinsIngestSettings:
    finished_event_type: str = "dev.cdevents.pipelinerun.finished.0.1.0"


@dataclass(frozen=True)
class ArchiveSettings:
    build_limit: int = 100
    log_tail_bytes: int = 524288
    gcs_bucket: str | None = None
    gcs_prefix: str = ""


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "noop"
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class Settings:
    database: DatabaseSettings
    jobs: JobSettings
    kafka: KafkaSettings = KafkaSettings()
    jenkins: JenkinsSettings = JenkinsSettings()
    jenkins_ingest: JenkinsIngestSettings = JenkinsIngestSettings()
    archive: ArchiveSettings = ArchiveSettings()
    llm: LLMSettings = LLMSettings()
    log_level: str = "INFO"


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    database_url = env.get("CI_DASHBOARD_DB_URL") or None
    if database_url:
        database = DatabaseSettings(
            url=database_url,
            host=None,
            port=None,
            user=None,
            password=None,
            database=None,
            ssl_ca=env.get("TIDB_SSL_CA") or None,
        )
    else:
        database = DatabaseSettings(
            url=None,
            host=_read_required(env, "TIDB_HOST"),
            port=_read_int(env, "TIDB_PORT", 4000),
            user=_read_required(env, "TIDB_USER"),
            password=_read_required(env, "TIDB_PASSWORD"),
            database=_read_required(env, "TIDB_DB"),
            ssl_ca=env.get("TIDB_SSL_CA") or None,
        )
    return Settings(
        database=database,
        jobs=JobSettings(
            batch_size=_read_int(env, "CI_DASHBOARD_BATCH_SIZE", 1000),
            refresh_group_batch_size=_read_int(
                env,
                "CI_DASHBOARD_REFRESH_GROUP_BATCH_SIZE",
                25,
            ),
            refresh_build_limit=_read_int(
                env,
                "CI_DASHBOARD_REFRESH_BUILD_LIMIT",
                5000,
            ),
        ),
        kafka=KafkaSettings(
            bootstrap_servers=_read_csv(env, "CI_DASHBOARD_KAFKA_BOOTSTRAP_SERVERS", ()),
            jenkins_events_topic=(
                env.get("CI_DASHBOARD_KAFKA_JENKINS_EVENTS_TOPIC") or "jenkins-event"
            ),
            jenkins_consumer_group=(
                env.get("CI_DASHBOARD_KAFKA_JENKINS_GROUP_ID") or "ci-dashboard-v3-jenkins-worker"
            ),
            poll_timeout_ms=_read_int(env, "CI_DASHBOARD_KAFKA_POLL_TIMEOUT_MS", 1000),
        ),
        jenkins=JenkinsSettings(
            internal_base_url=env.get("CI_DASHBOARD_JENKINS_INTERNAL_BASE_URL") or None,
            username=env.get("CI_DASHBOARD_JENKINS_USERNAME") or None,
            api_token=env.get("CI_DASHBOARD_JENKINS_API_TOKEN") or None,
            http_timeout_seconds=_read_int(env, "CI_DASHBOARD_JENKINS_HTTP_TIMEOUT_SECONDS", 30),
            progressive_probe_start=_read_int(
                env,
                "CI_DASHBOARD_JENKINS_PROGRESSIVE_PROBE_START",
                2147483647,
            ),
        ),
        jenkins_ingest=JenkinsIngestSettings(
            finished_event_type=(
                env.get("CI_DASHBOARD_JENKINS_FINISHED_EVENT_TYPE")
                or "dev.cdevents.pipelinerun.finished.0.1.0"
            ),
        ),
        archive=ArchiveSettings(
            build_limit=_read_int(env, "CI_DASHBOARD_ARCHIVE_BUILD_LIMIT", 100),
            log_tail_bytes=_read_int(env, "CI_DASHBOARD_ARCHIVE_LOG_TAIL_BYTES", 524288),
            gcs_bucket=env.get("CI_DASHBOARD_GCS_BUCKET") or None,
            gcs_prefix=(env.get("CI_DASHBOARD_GCS_PREFIX") or "").strip("/"),
        ),
        llm=LLMSettings(
            provider=(env.get("CI_DASHBOARD_LLM_PROVIDER") or "noop").strip() or "noop",
            model=env.get("CI_DASHBOARD_LLM_MODEL") or None,
            api_key=env.get("CI_DASHBOARD_LLM_API_KEY") or None,
            base_url=env.get("CI_DASHBOARD_LLM_BASE_URL") or None,
            reasoning_effort=env.get("CI_DASHBOARD_LLM_REASONING_EFFORT") or None,
        ),
        log_level=(env.get("CI_DASHBOARD_LOG_LEVEL") or "INFO").upper(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
