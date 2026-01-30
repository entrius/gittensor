# Gittensor Issue CLI Reference

CLI commands for the Gittensor Issue Competition system.

## Command Structure

```
gitt issue (alias: i)           - Top-level mutation commands
gitt issue view (alias: v)      - Read commands (contract + API)
gitt issue val                  - Validator consensus commands
gitt issue admin (alias: a)     - Owner-only commands
```

## Configuration

The CLI reads from `~/.gittensor/config.json`:

```json
{
  "contract_address": "5Cxxx...",
  "ws_endpoint": "ws://localhost:9944",
  "api_url": "http://localhost:3000",
  "network": "local",
  "wallet": "default",
  "hotkey": "default"
}
```

**Priority:** CLI arguments > config file

**Manage via:** `gitt config <key> <value>`

| Key | Description |
|-----|-------------|
| `wallet` | Default wallet name |
| `hotkey` | Default hotkey name |
| `network` | Network (local, testnet, mainnet) |
| `ws_endpoint` | WebSocket RPC endpoint |
| `api_url` | Gittensor API URL |
| `contract_address` | Smart contract address (protected) |

**Note:** `contract_address` requires confirmation to change. This is set automatically by deployment scripts.

## Command Reference

| CLI Command | Alias | Source | Access |
|-------------|-------|--------|--------|
| **Mutations (`gitt issue` / `gitt i`)** | | | |
| `gitt issue register` | `gitt i register` | Contract | Owner |
| `gitt issue harvest` | `gitt i harvest` | Contract | Permissionless |
| `gitt issue deposit` | `gitt i deposit` | Contract | Permissionless |
| `gitt issue prefer` | `gitt i prefer` | Local | Miner |
| `gitt issue enroll` | `gitt i enroll` | Local | Miner |
| `gitt issue withdraw` | `gitt i withdraw` | Local | Miner |
| **Views (`gitt issue view` / `gitt i v`)** | | | |
| `gitt issue view issues` | `gitt i v issues` | Contract | Read |
| `gitt issue view bounty-pool` | `gitt i v bounty-pool` | Contract | Read |
| `gitt issue view pending-harvest` | `gitt i v pending-harvest` | Contract | Read |
| `gitt issue view issue <id>` | `gitt i v issue` | Contract | Read |
| `gitt issue view competition <id>` | `gitt i v competition` | Contract | Read |
| `gitt issue view competition-proposal <id>` | `gitt i v competition-proposal` | Contract | Read |
| `gitt issue view config` | `gitt i v config` | Contract | Read |
| `gitt issue view active-competitions` | `gitt i v active-competitions` | Contract | Read |
| `gitt issue view status` | `gitt i v status` | API+Local | Read |
| `gitt issue view elo` | `gitt i v elo` | API | Read |
| `gitt issue view competitions` | `gitt i v competitions` | API | Read |
| `gitt issue view leaderboard` | `gitt i v leaderboard` | API | Read |
| **Validator (`gitt issue val` - no short alias)** | | | |
| `gitt issue val propose-competition` | `gitt i val propose` | Contract | Validator |
| `gitt issue val vote-solution` | `gitt i val solution` | Contract | Validator |
| `gitt issue val vote-timeout` | `gitt i val timeout` | Contract | Validator |
| `gitt issue val vote-cancel-issue` | `gitt i val cancel` | Contract | Validator |
| **Admin (`gitt issue admin` / `gitt i a`)** | | | |
| `gitt issue admin cancel` | `gitt i a cancel` | Contract | Owner |
| `gitt issue admin payout` | `gitt i a payout` | Contract | Owner |
| `gitt issue admin set-owner` | `gitt i a set-owner` | Contract | Owner |
| `gitt issue admin set-treasury` | `gitt i a set-treasury` | Contract | Owner |
| `gitt issue admin set-validator` | `gitt i a set-validator` | Contract | Owner |
| `gitt issue admin set-config` | `gitt i a set-config` | Contract | Owner |
