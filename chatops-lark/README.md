# ChatOps Lark Bot

## Debug or Run locally

You can run it by following steps:

1. Prepare the configuration file `config.yaml`, example:
  ```yaml
  cherry-pick-invite.audit_webhook: <your_audit_lark_webhook>
  cherry-pick-invite.github_token: <your_github_token>
  ask.llm.system_prompt: <your_system_prompt>
  ask.llm.model: <your_model>
  ask.llm.azure_config:
    api_key: <your_azure_api_key>
    base_url: <your_azure_base_url>
    api_version: <your_azure_api_version>
  ask.llm.mcp_servers:
    <a-mcp-tool-name>:
      base_url: <your_mcp_server_base_url(without /sse path)>
  ```
2. Run the lark bot app:
  ```bash
  go run ./cmd/server -app-id=<your_app_id> -app-secret=<your_app_secret>
  ```

## Deployment

We are deploying the bot with GitOps using FluxCD, the manifest is in the [`chatops-lark` directory in `ee-ops`](https://github.com/PingCAP-QE/ee-ops/tree/main/apps/prod/chatops-lark).
So if you want to bump the image or update the configuration, you contribute a PR to the repository.
