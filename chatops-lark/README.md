# ChatOps Lark Bot

## Debug or Run locally

You can run it by following steps:

1. Prepare the configuration file `config.yaml`. An example configuration file is provided at `config.yaml.example`:
  ```yaml
  # Bot configuration
  bot_name: "ChatOps Bot"  # Optional: will be automatically fetched from API if not provided

  # Cherry pick configuration
  cherry_pick_invite:
    audit_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
    github_token: "ghp_xxx"

  # Ask command configuration
  ask:
    llm:
      azure_config:
        api_key: "your-api-key"
        base_url: "https://your-deployment.openai.azure.com"
        api_version: "2023-05-15"
      model: "gpt-4"
      system_prompt: "You are a helpful assistant for PingCAP employees."
      mcp_servers:
        tidb_server:
          base_url: "https://mcp-server.example.com"

  # DevBuild configuration
  devbuild:
    api_url: "https://tibuild.pingcap.net/api/devbuilds"
  ```

2. Run the lark bot app:
  ```bash
  go run ./cmd/server -app-id=<your_app_id> -app-secret=<your_app_secret>
  ```

## Deployment

We are deploying the bot with GitOps using FluxCD, the manifest is in the [`chatops-lark` directory in `ee-ops`](https://github.com/PingCAP-QE/ee-ops/tree/main/apps/prod/chatops-lark).
So if you want to bump the image or update the configuration, you contribute a PR to the repository.
