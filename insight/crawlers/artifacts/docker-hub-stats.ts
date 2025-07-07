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
  } catch (error: any) {
    return {
      error: `Failed to fetch data: ${error.message}`,
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
}

if (import.meta.main) {
  await main();
  Deno.exit(0);
}
