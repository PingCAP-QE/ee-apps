import { parseArgs } from "jsr:@std/cli@1.0.14/parse-args";
import * as mysql from "https://deno.land/x/mysql@v2.12.1/mod.ts";
import { walk } from "jsr:@std/fs@1.0.18/walk";
import ProgressBar from "jsr:@deno-library/progress";
import { convertDsnToClientConfig } from "../db/utils.ts";

export async function createTable(client: mysql.Client, tableName: string) {
  const sql = `
    CREATE TABLE IF NOT EXISTS \`${tableName}\` (
      id INT AUTO_INCREMENT PRIMARY KEY,
      type ENUM('issue', 'pull') NOT NULL,
      repo VARCHAR(255) NOT NULL,
      number INT NOT NULL,
      title VARCHAR(512) NOT NULL,
      author VARCHAR(128) NOT NULL,
      email VARCHAR(128),
      state ENUM('open', 'closed') NOT NULL,
      created_at DATETIME NOT NULL,
      updated_at DATETIME NOT NULL,
      closed_at DATETIME,
      closed_by VARCHAR(255),
      assignee VARCHAR(255),
      assignees JSON,
      labels JSON,
      comments JSON,
      merged BOOLEAN,
      merged_at DATETIME,
      merged_by VARCHAR(128),
      additions INT,
      deletions INT,
      changed_files INT,
      commit_count INT,
      commits JSON,
      review JSON,
      review_comments JSON,
      INDEX (repo, number)
    )
  `;

  await client.execute(sql);
}

export async function saveTicket(
  client: mysql.Client,
  tableName: string,
  job: Record<string, any>,
) {
  const insertOrUpdateSql = `
      INSERT INTO \`${tableName}\` (
        type, repo, number, title, author, email, state, created_at, updated_at,
        closed_at, closed_by, assignee, assignees, labels, comments, merged,
        merged_at, merged_by, additions, deletions, changed_files, commit_count,
        commits, review, review_comments
      ) VALUES (
      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
      ) ON DUPLICATE KEY UPDATE
      type = VALUES(type),
      repo = VALUES(repo),
      number = VALUES(number),
      title = VALUES(title),
      author = VALUES(author),
      email = VALUES(email),
      state = VALUES(state),
      created_at = VALUES(created_at),
      updated_at = VALUES(updated_at),
      closed_at = VALUES(closed_at),
      closed_by = VALUES(closed_by),
      assignee = VALUES(assignee),
      assignees = VALUES(assignees),
      labels = VALUES(labels),
      comments = VALUES(comments),
      merged = VALUES(merged),
      merged_at = VALUES(merged_at),
      merged_by = VALUES(merged_by),
      additions = VALUES(additions),
      deletions = VALUES(deletions),
      changed_files = VALUES(changed_files),
      commit_count = VALUES(commit_count),
      commits = VALUES(commits),
      review = VALUES(review),
      review_comments = VALUES(review_comments)
    `;

  await client.execute(insertOrUpdateSql, githubTicketInsertValues(job));
}

function githubTicketInsertValues(data: Record<string, any>) {
  return [
    data.type,
    data.repo,
    data.number,
    data.title,
    data.author,
    data.email,
    data.state,
    data.created_at,
    data.updated_at,
    data.closed_at,
    data.closed_by,
    data.assignee,
    JSON.stringify(data.assignees),
    JSON.stringify(data.labels),
    JSON.stringify(data.comments),
    data.merged,
    data.merged_at,
    data.merged_by,
    data.additions,
    data.deletions,
    data.changed_files,
    data.commit_count,
    JSON.stringify(data.commits),
    JSON.stringify(data.review),
    JSON.stringify(data.review_comments),
  ];
}

async function transform(
  folderPath: string,
  client: mysql.Client,
  table: string,
) {
  const files = [];
  for await (const entry of walk(folderPath)) {
    if (
      entry.isFile && /^\d+\.json$/.test(entry.name) &&
        entry.path.includes("/pulls/") || entry.path.includes("/issues/")
    ) {
      files.push(entry.path);
    }
  }

  const progressBar = new ProgressBar({
    total: files.length,
    title: "Progress:",
    width: 800,
    display: ":title [:bar] :percent | ETA: :eta | Completed :completed/:total",
    prettyTime: true,
    complete: "=",
    incomplete: "-",
  });

  let completed = 0;
  for (const file of files) {
    await progressBar.console(`Importing file: ${file}`);
    const data = JSON.parse(await Deno.readTextFile(file));
    await saveTicket(client, table, data);
    await progressBar.render(completed++);
  }
}

async function main() {
  const args = parseArgs(Deno.args, {
    string: ["path", "dsn", "table"],
    default: {
      tls: true,
    },
    negatable: ["tls"],
  });

  // check the args, if they are valid
  if (!(args.path && args.dsn && args.table)) {
    console.error(
      "Usage: script --path <folder-path> --dsn <dsn> --table <tableName> [--no-tls]",
    );
    Deno.exit(1);
  }

  // connect to the database.
  const config = convertDsnToClientConfig(args.dsn!);
  if (args.tls) {
    config.tls = { mode: mysql.TLSMode.VERIFY_IDENTITY };
  }
  const db = await new mysql.Client().connect(config);
  await createTable(db, args.table); // create it if not exists.
  await transform(args.path, db, args.table);
  await db.close();
}

if (import.meta.main) {
  await main();
}
