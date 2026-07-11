import { parseArgs } from "jsr:@std/cli@1.0.14/parse-args";
import * as mysql from "https://deno.land/x/mysql@v2.12.1/mod.ts";
import { convertDsnToClientConfig } from "../../db/utils.ts";

const VALID_JOB_STATES = new Set([
  "triggered",
  "pending",
  "success",
  "failure",
  "error",
  "aborted",
]);

const VALID_JOB_TYPES = new Set([
  "presubmit",
  "postsubmit",
  "batch",
  "periodic",
]);

export interface prowJobRun {
  kind: string;
  metadata: {
    name?: string;
    namespace?: string;
    labels?: Record<string, string>;
  };
  spec: {
    type?: string;
    agent?: string;
    cluster?: string;
    namespace?: string;
    job?: string;
    report?: boolean;
    refs?: {
      org: string;
      repo: string;
      base_ref: string;
      pulls?: {
        number: number;
        author: string;
        sha: string;
      }[];
    };
    extra_refs?: {
      org: string;
      repo: string;
      base_ref: string;
    };
  };
  status: {
    state?: string | null;
    startTime?: string | null;
    pendingTime?: string | null;
    completionTime?: string | null;
    url?: string | null;
  };
}

export async function fetchProwJobs(prowBaseUrl: string) {
  const apiUrl =
    `${prowBaseUrl}/prowjobs.js?omit=annotations,decoration_config,pod_spec`;

  const res = await fetch(apiUrl);
  const data = await res.json() as { items: prowJobRun[] };
  return data.items;
}

function hasValidDate(value: string | null | undefined): value is string {
  if (!value) {
    return false;
  }
  return !Number.isNaN(new Date(value).getTime());
}

export function invalidProwJobReason(job: prowJobRun): string | null {
  if (!job.metadata?.namespace) {
    return "missing metadata.namespace";
  }
  if (!job.metadata?.name) {
    return "missing metadata.name";
  }
  if (!job.spec?.job) {
    return "missing spec.job";
  }
  if (!job.spec?.type) {
    return "missing spec.type";
  }
  if (!VALID_JOB_TYPES.has(job.spec.type)) {
    return `invalid spec.type: ${job.spec.type}`;
  }
  if (!job.status?.state) {
    return "missing status.state";
  }
  if (!VALID_JOB_STATES.has(job.status.state)) {
    return `invalid status.state: ${job.status.state}`;
  }
  if (!hasValidDate(job.status.startTime)) {
    return "missing or invalid status.startTime";
  }
  return null;
}

export function filterInsertableJobs(jobs: prowJobRun[]) {
  const insertableJobs: prowJobRun[] = [];
  const skippedJobs: { job: prowJobRun; reason: string }[] = [];

  for (const job of jobs) {
    const reason = invalidProwJobReason(job);
    if (reason) {
      skippedJobs.push({ job, reason });
      continue;
    }
    insertableJobs.push(job);
  }

  return { insertableJobs, skippedJobs };
}

export async function createJobTable(client: mysql.Client, tableName: string) {
  // Validate table name to prevent SQL injection (only allow alphanumeric and underscores)
  if (!/^[a-zA-Z0-9_]+$/.test(tableName)) {
    throw new Error(
      `Invalid table name: ${tableName}. Only alphanumeric characters and underscores are allowed.`,
    );
  }

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
      retest BOOLEAN DEFAULT NULL,
      author VARCHAR(128),
      event_guid VARCHAR(128),
      spec JSON,
      status JSON,
      PRIMARY KEY (prowJobId)
    );
  `;

  await client.execute(sql);
}

export async function migrateJobTable(client: mysql.Client, tableName: string) {
  // Validate table name to prevent SQL injection (only allow alphanumeric and underscores)
  if (!/^[a-zA-Z0-9_]+$/.test(tableName)) {
    throw new Error(
      `Invalid table name: ${tableName}. Only alphanumeric characters and underscores are allowed.`,
    );
  }

  // Check if table exists
  const tableExistsResult = await client.query(
    `SELECT COUNT(*) as count FROM information_schema.tables
     WHERE table_schema = DATABASE() AND table_name = ?`,
    [tableName],
  );

  if (
    !tableExistsResult || tableExistsResult.length === 0 ||
    tableExistsResult[0].count === 0
  ) {
    console.info(`Table ${tableName} does not exist, skipping migration`);
    return;
  }

  // Get existing columns
  const columnsResult = await client.query(
    `SELECT COLUMN_NAME FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = ?`,
    [tableName],
  );

  const existingColumns = new Set(
    columnsResult.map((row: { COLUMN_NAME: string }) => row.COLUMN_NAME),
  );

  // Add retest column if it doesn't exist
  if (!existingColumns.has("retest")) {
    console.info(`Adding column 'retest' to table ${tableName}`);
    await client.execute(
      `ALTER TABLE \`${tableName}\` ADD COLUMN retest BOOLEAN DEFAULT NULL AFTER url`,
    );
  }

  // Add author column if it doesn't exist
  if (!existingColumns.has("author")) {
    console.info(`Adding column 'author' to table ${tableName}`);
    await client.execute(
      `ALTER TABLE \`${tableName}\` ADD COLUMN author VARCHAR(128) AFTER retest`,
    );
  }

  // Add event_guid column if it doesn't exist
  if (!existingColumns.has("event_guid")) {
    console.info(`Adding column 'event_guid' to table ${tableName}`);
    await client.execute(
      `ALTER TABLE \`${tableName}\` ADD COLUMN event_guid VARCHAR(128) AFTER author`,
    );
  }

  console.info(`Migration completed for table ${tableName}`);
}

function jobInsertValues(job: prowJobRun) {
  const labels = job.metadata.labels ?? {};

  // Helper to parse the retest label into a nullable boolean
  const parseRetestLabel = (label: string | undefined): boolean | null => {
    if (label === "true") return true;
    if (label === "false") return false;
    return null;
  };

  // Helper to get author from spec.refs.pulls[0].author for presubmit jobs
  // Note: Only presubmit jobs have pulls array with author information
  const getAuthor = (): string | null => {
    if (job.spec.type === "presubmit" && job.spec.refs?.pulls?.[0]?.author) {
      return job.spec.refs.pulls[0].author;
    }
    return null;
  };

  return [
    job.metadata.namespace!,
    job.metadata.name!,
    job.spec.job!,
    job.spec.type!,
    job.status.state!,
    new Date(job.status.startTime!),
    job.status.completionTime ? new Date(job.status.completionTime) : null,
    labels["prow.k8s.io/is-optional"] === "true",
    job.spec.report || false,
    job.spec.refs?.org || null,
    job.spec.refs?.repo || null,
    job.spec.refs?.base_ref || null,
    labels["prow.k8s.io/refs.pull"] || null,
    labels["prow.k8s.io/context"] || null,
    job.status.url || null,
    parseRetestLabel(labels["prow.k8s.io/retest"]),
    getAuthor(),
    labels["event-GUID"] || null,
    JSON.stringify(job.spec),
    JSON.stringify(job.status),
  ];
}

export async function saveJobs(
  client: mysql.Client,
  tableName: string,
  jobs: prowJobRun[],
  chunkSize = 100, // Default chunk size
) {
  const { insertableJobs, skippedJobs } = filterInsertableJobs(jobs);

  if (skippedJobs.length > 0) {
    console.warn(
      `Skipping ${skippedJobs.length}/${jobs.length} prow jobs with incomplete data`,
    );
    for (const { job, reason } of skippedJobs.slice(0, 5)) {
      console.warn(
        `Skipped prow job ${job.metadata?.name ?? "<unknown>"}: ${reason}`,
      );
    }
  }

  for (let i = 0; i < insertableJobs.length; i += chunkSize) {
    const chunk = insertableJobs.slice(i, i + chunkSize);
    const values = chunk.map((job) => jobInsertValues(job));
    const placeholders = values.map((value) =>
      `(${value.map(() => "?").join(", ")})`
    ).join(", ");

    const sql = `
      INSERT INTO \`${tableName}\` (
        namespace, prowJobId, jobName, type, state, startTime, completionTime, optional, report, org, repo, base_ref, pull, context, url, retest, author, event_guid, spec, status
      ) VALUES ${placeholders}
      ON DUPLICATE KEY UPDATE
        state = VALUES(state),
        startTime = VALUES(startTime),
        completionTime = VALUES(completionTime),
        url = VALUES(url),
        retest = VALUES(retest),
        author = VALUES(author),
        event_guid = VALUES(event_guid),
        status = VALUES(status);
    `;
    const flattenedValues = values.flat();
    await client.execute(sql, flattenedValues);
    console.info(
      `Saved ${
        Math.min(i + chunkSize, insertableJobs.length)
      }/${insertableJobs.length} jobs`,
    );
  }
}

async function main() {
  const args = parseArgs(Deno.args, {
    string: ["dsn", "table", "prow_base_url", "chunk_size"],
    default: {
      tls: true,
      table: "prow_jobs",
      prow_base_url: "https://prow.tidb.net",
      chunk_size: "100",
    },
    negatable: ["tls"],
  });

  // check the args, if they are valid
  if (!(args.dsn && args.table && args.prow_base_url)) {
    console.error(
      "Usage: script --dsn <dsn> --table <tableName> --prow_base_url <prowBaseUrl> [--chunk_size <size>] [--no-tls]",
    );
    Deno.exit(1);
  }

  const chunkSize = parseInt(args.chunk_size, 10);
  if (isNaN(chunkSize) || chunkSize <= 0) {
    console.error("Invalid chunk_size. It must be a positive integer.");
    Deno.exit(1);
  }

  // connect to the database.
  const config = convertDsnToClientConfig(args.dsn!);
  if (args.tls) {
    config.tls = { mode: mysql.TLSMode.VERIFY_IDENTITY };
  }
  const db = await new mysql.Client().connect(config);
  await createJobTable(db, args.table); // create it if not exists.
  await migrateJobTable(db, args.table); // migrate existing table to add new columns.

  console.group("Fetching jobs and saving to database...");
  const jobs = await fetchProwJobs(args.prow_base_url);
  console.info("fetched job count:", jobs.length);
  console.info("Saving the jobs to the table:", args.table);
  await saveJobs(db, args.table, jobs, chunkSize);
  console.info("Jobs saved successfully");
  console.groupEnd();
  await db.close();
}

if (import.meta.main) {
  await main();
}
