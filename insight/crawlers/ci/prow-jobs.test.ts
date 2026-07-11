import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.19";
import { convertDsnToClientConfig } from "../../db/utils.ts";
import {
  filterInsertableJobs,
  invalidProwJobReason,
  type prowJobRun,
} from "./prow-jobs.ts";

function makeProwJob(overrides: Partial<prowJobRun> = {}): prowJobRun {
  const job: prowJobRun = {
    kind: "ProwJob",
    metadata: {
      name: "valid-prow-job",
      namespace: "prow-test-pods",
      labels: {},
    },
    spec: {
      type: "periodic",
      agent: "kubernetes",
      cluster: "default",
      namespace: "prow-test-pods",
      job: "periodic-crawl-ci-run-data",
      report: true,
    },
    status: {
      state: "success",
      startTime: "2026-07-11T02:11:48Z",
      pendingTime: "2026-07-11T02:11:48Z",
      completionTime: "2026-07-11T02:14:10Z",
      url: "https://prow.tidb.net/view/gs/prow-tidb-logs/logs/example/1",
    },
  };

  return {
    ...job,
    ...overrides,
    metadata: { ...job.metadata, ...overrides.metadata },
    spec: { ...job.spec, ...overrides.spec },
    status: { ...job.status, ...overrides.status },
  };
}

Deno.test("should correctly parse a valid DSN", () => {
  const dsn = "mysql://user:password@localhost:5432/database";
  const config = convertDsnToClientConfig(dsn);

  assertEquals(config, {
    hostname: "localhost",
    port: 5432,
    username: "user",
    password: "password",
    db: "database",
  });
});

Deno.test("should throw an error if DSN is missing user and password", () => {
  const dsn = "@localhost:5432/database";

  assertThrows(() => convertDsnToClientConfig(dsn));
});

Deno.test("should throw an error if DSN is missing host and port", () => {
  const dsn = "mysql://user:password@/database";

  assertThrows(() => convertDsnToClientConfig(dsn));
});

Deno.test("should throw an error if DSN is missing database", () => {
  const dsn = "mysql://user:password@localhost:5432/";

  assertThrows(() => convertDsnToClientConfig(dsn));
});

Deno.test("should correctly parse a DSN with special characters in user and password", () => {
  const dsn = "mysql://user%40name:pass%3Aword@localhost:5432/database";
  const config = convertDsnToClientConfig(dsn);

  assertEquals(config, {
    hostname: "localhost",
    port: 5432,
    username: "user@name",
    password: "pass:word",
    db: "database",
  });
});

Deno.test("filters prow jobs missing insert-required status fields", () => {
  const validJob = makeProwJob();
  const missingStateJob = makeProwJob({
    metadata: {
      name: "missing-state",
      namespace: "prow-test-pods",
      labels: {},
    },
    status: { state: null },
  });
  const missingStartTimeJob = makeProwJob({
    metadata: {
      name: "missing-start-time",
      namespace: "prow-test-pods",
      labels: {},
    },
    status: { startTime: null },
  });

  const { insertableJobs, skippedJobs } = filterInsertableJobs([
    validJob,
    missingStateJob,
    missingStartTimeJob,
  ]);

  assertEquals(insertableJobs.map((job) => job.metadata.name), [
    "valid-prow-job",
  ]);
  assertEquals(skippedJobs.map(({ reason }) => reason), [
    "missing status.state",
    "missing or invalid status.startTime",
  ]);
});

Deno.test("rejects prow jobs with states outside the database enum", () => {
  const job = makeProwJob({
    status: { state: "unknown-state" },
  });

  assertEquals(
    invalidProwJobReason(job),
    "invalid status.state: unknown-state",
  );
});
