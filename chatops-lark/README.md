# ChatOps Lark Bot

## Debug or Run locally

You can run it by following steps:

1. Prepare the configuration file `config.yaml`, example:
  ```yaml
  cherry-pick-invite.audit_webhook: <your_audit_lark_webhook>
  cherry-pick-invite.github_token: <your_github_token>
  bot_name: <your_bot_name>  # bot name in lark
  ```
2. Run the lark bot app:
  ```bash
  go run ./cmd/server -app-id=<your_app_id> -app-secret=<your_app_secret>
  ```

## Deployment

We are deploying the bot with GitOps using FluxCD, the manifest is in the [`chatops-lark` directory in `ee-ops`](https://github.com/PingCAP-QE/ee-ops/tree/main/apps/prod/chatops-lark).
So if you want to bump the image or update the configuration, you contribute a PR to the repository.
