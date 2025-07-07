async function getDockerRepoStats(hubImg: string) {
  const url = `https://hub.docker.com/v2/repositories/${hubImg}/`;

  try {
    const response = await fetch(url);

    if (!response.ok) {
      throw new Error(`HTTP error! Status: ${response.status}`);
    }

    const stats = await response.json();
    const { date_registered, pull_count, star_count, storage_size } = stats;
    return {
      repo: `docker.io/${hubImg}`,
      date_registered,
      pull_count,
      star_count,
      storage_size,
    };
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    return {
      error: `Failed to fetch data: ${message}`,
    };
  }
}

async function main() {
  const images = [
    "pingcap/br",
    "pingcap/dm",
    "pingcap/dumpling",
    "pingcap/pd",
    "pingcap/ticdc",
    "pingcap/tidb",
    "pingcap/tidb-lightning",
    "pingcap/tiflash",
    "pingcap/tikv",
    "pingcap/tidb-enterprise",
    "pingcap/tikv-enterprise",
  ];

  const results = await Promise.all(images.map(getDockerRepoStats));
  console.dir(results);

  const hasErrors = results.some((r) => "error" in r);
  if (hasErrors) {
    console.error("\nAn error occurred while fetching stats for some images.");
    Deno.exit(1);
  }
}

if (import.meta.main) {
  await main();
}
