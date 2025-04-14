import { Octokit } from "https://esm.sh/octokit@4.0.2?dts";
import { parseArgs } from "jsr:@std/cli/parse-args";
import * as csv from "jsr:@std/csv";

export interface PullRequestData {
  number: number;
  title: string;
  author: string;
  state: string;
  created_at: string;
  merged_at: string | null;
  updated_at: string;
}

export class GitHubService {
  private octokit: Octokit;

  constructor(token: string) {
    this.octokit = new Octokit({ auth: token });
  }

  async fetchAllPullRequests(
    owner: string,
    repo: string,
  ): Promise<PullRequestData[]> {
    return await this.octokit
      .paginate(this.octokit.rest.pulls.list, {
        owner,
        repo,
        state: "all",
        per_page: 100,
      });
  }
}

export class CsvService {
  private static readonly HEADERS = [
    "number",
    "author",
    "state",
    "created_at",
    "updated_at",
    "merged_at",
    "title",
  ];

  async savePullRequestsToCSV(
    pullRequests: PullRequestData[],
    filename: string,
  ): Promise<void> {
    try {
      const rows = pullRequests.map((pr) => [
        pr.number,
        pr.user.login,
        pr.state,
        pr.created_at,
        pr.updated_at,
        pr.merged_at,
        pr.title,
      ]);

      rows.unshift(CsvService.HEADERS);
      const csvContent = csv.stringify(rows);
      await Deno.writeTextFile(filename, csvContent);
    } catch (error) {
      throw new Error(`Failed to save CSV file: ${error}`);
    }
  }
}

async function main() {
  const flags = parseArgs(Deno.args, { string: ["owner", "repo", "token"] });

  if (!flags.owner || !flags.repo || !flags.token) {
    throw new Error(
      "Usage: deno run main.ts --owner=<owner> --repo=<repo> --token=<github_token>",
    );
  }

  console.log(`Fetching pull requests for ${flags.owner}/${flags.repo}...`);

  // Initialize services
  const githubService = new GitHubService(flags.token);
  const csvService = new CsvService();

  // Fetch pull requests
  const pullRequests = await githubService.fetchAllPullRequests(
    flags.owner,
    flags.repo,
  );
  console.log(`Found ${pullRequests.length} pull requests`);

  // Save to CSV
  const filename = `${flags.owner}-${flags.repo}-prs.csv`;
  await csvService.savePullRequestsToCSV(pullRequests, filename);
  console.log(`Pull requests saved to ${filename}`);
}

// Run the application
if (import.meta.main) {
  await main();
  Deno.exit(0);
}
