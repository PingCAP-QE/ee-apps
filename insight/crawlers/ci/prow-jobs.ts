import { parseArgs } from "jsr:@std/cli@1.0.14/parse-args";
import * as mysql from "https://deno.land/x/mysql@v2.12.1/mod.ts";
import { convertDsnToClientConfig } from "../../db/utils.ts";
interface prowJobRun {
  kind: string;
  metadata: {
    name: string;
    namespace: string;
    labels: Record<string, string>;
  };
  spec: {
    type: string;
    agent: string;
    cluster: string;
    namespace: string;
    job: string;
    report: boolean;
    refs?: {
      org: string;
      repo: string;
      base_ref: string;
    };
    extra_refs?: {
      org: string;
      repo: string;
      base_ref: string;
    };
  };
  status: {
    state: string;
    startTime: string;
    pendingTime: string;
    completionTime: string;
    url: string;
  };
}

export async function fetchProwJobs(prowBaseUrl: string) {
  const apiUrl =
    `${prowBaseUrl}/prowjobs.js?omit=annotations,decoration_config,pod_spec`;

  const res = await fetch(apiUrl);
  const data = await res.json() as { items: prowJobRun[] };
  return data.items;
}

export async function createJobTable(client: mysql.Client, tableName: string) {
  const sql = `
    CREATE TABLE IF NOT EXISTS \`${tableName}\` (
      id INT AUTO_INCREMENT,
      namespace VARCHAR(255) NOT NULL,
      prowJobId CHAR(36) NOT NULL UNIQUE,
      jobName VARCHAR(255) NOT NULL,
      type ENUM('presubmit', 'postsubmit', 'batch', 'periodic') NOT NULL,
      state ENUM('triggered','pending', 'success', 'failure', 'error', 'aborted') NOT NULL,
      startTime DATETIME NOT NULL,
      completionTime DATETIME,
      optional BOOLEAN NOT NULL,
      report BOOLEAN NOT NULL,
      org VARCHAR(63),
      repo VARCHAR(63),
      base_ref VARCHAR(63),
      pull INT,
      context VARCHAR(128),
      url VARCHAR(255),
      spec JSON,
      status JSON,
      PRIMARY KEY (prowJobId)
    );
  `;

  await client.execute(sql);
}

function jobInsertValues(job: prowJobRun) {
  return [
    job.metadata.namespace,
    job.metadata.name,
    job.spec.job,
    job.spec.type,
    job.status.state,
    new Date(job.status.startTime),
    job.status.completionTime ? new Date(job.status.completionTime) : null,
    job.metadata.labels["prow.k8s.io/is-optional"] === "true",
    job.spec.report || false,
    job.spec.refs?.org || null,
    job.spec.refs?.repo || null,
    job.spec.refs?.base_ref || null,
    job.metadata.labels["prow.k8s.io/refs.pull"] || null,
    job.metadata.labels["prow.k8s.io/context"] || null,
    job.status.url || null,
    JSON.stringify(job.spec),
    JSON.stringify(job.status),
  ];
}

export async function saveJobs(
  client: mysql.Client,
  tableName: string,
  jobs: prowJobRun[],
) {
  const values = jobs.map((job) => jobInsertValues(job));
  const placeholders = values.map((value) =>
    `(${value.map(() => "?").join(", ")})`
  ).join(", ");

  const sql = `
    INSERT INTO \`${tableName}\` (
      namespace, prowJobId, jobName, type, state, startTime, completionTime, optional, report, org, repo, base_ref, pull, context, url, spec, status
    ) VALUES ${placeholders}
    ON DUPLICATE KEY UPDATE
      state = VALUES(state),
      startTime = VALUES(startTime),
      completionTime = VALUES(completionTime),
      url = VALUES(url),
      status = VALUES(status);
  `;
  const flattenedValues = values.flat();
  await client.execute(sql, flattenedValues);
}

export async function saveJob(
  client: mysql.Client,
  tableName: string,
  job: prowJobRun,
) {
  const selectSql = `
      SELECT state FROM \`${tableName}\` WHERE prowJobId = ?;
    `;

  const insertOrUpdateSql = `
      INSERT INTO \`${tableName}\` (
        namespace, prowJobId, jobName, type, state, startTime, completionTime, optional, report, org, repo, base_ref, pull, context, url, spec, status
      ) VALUES (
        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
      ) ON DUPLICATE KEY UPDATE
        state = VALUES(state),
        startTime = VALUES(startTime),
        completionTime = VALUES(completionTime),
        url = VALUES(url),
        status = VALUES(status);
    `;

  const values = jobInsertValues(job);

  const [existingJob] = await client.query(selectSql, [job.metadata.name]);
  const shouldUpdateOrInsert = !existingJob ||
    (existingJob.state != job.status.state);
  if (!shouldUpdateOrInsert) {
    return;
  }

  await client.execute(insertOrUpdateSql, values);
}

async function main() {
  const args = parseArgs(Deno.args, {
    string: ["dsn", "table", "prow_base_url"],
    default: {
      tls: true,
      table: "prow_jobs",
      prow_base_url: "https://prow.tidb.net",
    },
    negatable: ["tls"],
  });

  // check the args, if they are valid
  if (!(args.dsn && args.table && args.prow_base_url)) {
    console.error(
      "Usage: script --dsn <dsn> --table <tableName> --prow_base_url <prowBaseUrl> [--no-tls]",
    );
    Deno.exit(1);
  }

  // connect to the database.
  const config = convertDsnToClientConfig(args.dsn!);
  if (args.tls) {
    config.tls = { mode: mysql.TLSMode.VERIFY_IDENTITY };
  }
  const db = await new mysql.Client().connect(config);
  await createJobTable(db, args.table); // create it if not exists.

  console.group("Fetching jobs and saving to database...");
  const jobs = await fetchProwJobs(args.prow_base_url);
  console.info("fetched job count:", jobs.length);
  console.info("Saving the jobs to the table:", args.table);
  await saveJobs(db, args.table, jobs);
  console.info("Jobs saved successfully");
  console.groupEnd();
  await db.close();
}

if (import.meta.main) {
  await main();
}
