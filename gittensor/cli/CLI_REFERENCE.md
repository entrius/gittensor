# Gittensor Issue CLI Reference

CLI commands for the Gittensor Issue Competition system.

## Command Structure

```
gitt issue (alias: i)           - Top-level mutation commands
gitt issue view (alias: v)      - Read commands (contract + API)
gitt issue val                  - Validator consensus commands
gitt issue admin (alias: a)     - Owner-only commands
```

## Quick Reference

| Full Command | Short Form | Description |
|--------------|------------|-------------|
| `gitt issue view issues` | `gitt i v issues` | List available issues |
| `gitt issue view bounty-pool` | `gitt i v bounty-pool` | View alpha pool balance |
| `gitt issue prefer 1 2 3` | `gitt i prefer 1 2 3` | Set preferences |
| `gitt issue val propose 1 <hk1> <hk2>` | `gitt i val propose ...` | Propose competition |

---

## Top-Level Commands (`gitt issue`)

### `gitt issue register`
Register a new issue with a bounty (OWNER ONLY).

```bash
gitt issue register --repo owner/repo --issue 1 --bounty 100
gitt issue register --repo tensorflow/tensorflow --issue 12345 --bounty 50 --testnet
```

| Option | Description |
|--------|-------------|
| `--repo` | Repository in owner/repo format (required) |
| `--issue` | GitHub issue number (required) |
| `--bounty` | Target bounty in ALPHA tokens (required) |
| `--contract` | Contract address (uses config if empty) |
| `--testnet` | Use testnet contract address |
| `--wallet-name` | Wallet name (must be contract owner) |
| `--wallet-hotkey` | Hotkey name |
| `--rpc-url` | Subtensor RPC endpoint |

### `gitt issue harvest`
Manually trigger emission harvest from contract treasury (permissionless).

```bash
gitt issue harvest
gitt issue harvest --verbose
gitt i harvest --wallet-name mywallet
```

| Option | Description |
|--------|-------------|
| `--wallet-name` | Wallet name |
| `--wallet-hotkey` | Hotkey name |
| `--contract` | Contract address (uses config if empty) |
| `--verbose` / `-v` | Show detailed output |
| `--rpc-url` | Subtensor RPC endpoint |

### `gitt issue deposit`
Deposit funds directly to an issue's bounty (anyone can fund).

```bash
gitt issue deposit 1 50.0
gitt i deposit 42 100
```

| Argument | Description |
|----------|-------------|
| `ISSUE_ID` | Issue to fund |
| `AMOUNT` | Amount in ALPHA tokens |

### `gitt issue prefer`
Set ranked issue preferences (most preferred first).

```bash
gitt issue prefer 42 15 8
gitt issue prefer 1 2 3 --clear
```

| Argument | Description |
|----------|-------------|
| `ISSUE_IDS` | Space-separated list of issue IDs in preference order |

| Option | Description |
|--------|-------------|
| `--clear` | Clear existing preferences before adding |

### `gitt issue enroll`
Quick enroll for a single issue (shorthand for prefer).

```bash
gitt issue enroll 42
gitt i enroll 123
```

### `gitt issue withdraw`
Clear issue preferences (stop competing for new issues).

```bash
gitt issue withdraw
gitt issue withdraw --force
```

| Option | Description |
|--------|-------------|
| `--force` / `-f` | Skip confirmation prompt |

---

## View Commands (`gitt issue view` / `gitt i v`)

All read-only commands for viewing contract state and API data.

### `gitt issue view issues`
List available issues for competition.

```bash
gitt issue view issues
gitt i v issues --testnet
gitt i v issues --verbose --from-api
```

| Option | Description |
|--------|-------------|
| `--testnet` | Use testnet contract address |
| `--from-api` | Force reading from API instead of contract |
| `--verbose` / `-v` | Show debug output |
| `--contract` | Contract address |
| `--rpc-url` | Subtensor RPC endpoint |

### `gitt issue view bounty-pool`
View current alpha pool balance.

```bash
gitt issue view bounty-pool
gitt i v bounty-pool --verbose
```

### `gitt issue view pending-harvest`
View pending emissions value (current stake on treasury).

```bash
gitt issue view pending-harvest
gitt i v pending-harvest
```

### `gitt issue view issue <ID>`
View raw issue data from contract.

```bash
gitt issue view issue 1
gitt i v issue 42 --verbose
```

### `gitt issue view competition <ID>`
View competition details from contract.

```bash
gitt issue view competition 1
gitt i v competition 42
```

### `gitt issue view competition-proposal <ISSUE_ID>`
View competition proposal state for an issue.

```bash
gitt issue view competition-proposal 1
gitt i v competition-proposal 42
```

### `gitt issue view config`
View contract configuration.

```bash
gitt issue view config
gitt i v config --verbose
```

### `gitt issue view active-competitions`
List all active competitions from contract.

```bash
gitt issue view active-competitions
gitt i v active-competitions
```

### `gitt issue view status`
View your current competition status (local + API).

```bash
gitt issue view status
gitt i v status --wallet-name mywallet
```

### `gitt issue view elo`
View your ELO rating and competition history.

```bash
gitt issue view elo
gitt i v elo --wallet-name mywallet
```

### `gitt issue view competitions`
View all competitions from API.

```bash
gitt issue view competitions
gitt i v competitions --limit 20
```

### `gitt issue view leaderboard`
View the ELO leaderboard.

```bash
gitt issue view leaderboard
gitt i v leaderboard --limit 25
```

---

## Validator Commands (`gitt issue val`)

Validator consensus operations for managing the competition lifecycle.

**Note:** The `val` subgroup has no short alias to avoid collision with `view`.

### `gitt issue val propose-competition` (alias: `propose`)
Propose a miner pair for competition (or vote if same pair already proposed).

```bash
gitt issue val propose-competition 1 5Hxxx... 5Hyyy...
gitt i val propose 42 <hotkey1> <hotkey2>
```

| Argument | Description |
|----------|-------------|
| `ISSUE_ID` | Issue to start competition for |
| `MINER1_HOTKEY` | First miner's hotkey |
| `MINER2_HOTKEY` | Second miner's hotkey |

### `gitt issue val vote-solution` (alias: `solution`)
Vote for a solution winner in an active competition (triggers auto-payout).

```bash
gitt issue val vote-solution 1 5Hxxx... 5Hyyy... https://github.com/.../pull/123
gitt i val solution 42 <hotkey> <coldkey> <pr_url>
```

| Argument | Description |
|----------|-------------|
| `COMPETITION_ID` | Competition to vote on |
| `WINNER_HOTKEY` | Winner's hotkey |
| `WINNER_COLDKEY` | Winner's coldkey (payout destination) |
| `PR_URL` | URL of the winning PR |

### `gitt issue val vote-timeout` (alias: `timeout`)
Vote to timeout an expired competition.

```bash
gitt issue val vote-timeout 1
gitt i val timeout 42
```

| Argument | Description |
|----------|-------------|
| `COMPETITION_ID` | Competition to timeout |

### `gitt issue val vote-cancel-issue` (alias: `cancel`)
Vote to cancel an issue (works on Registered, Active, or InCompetition).

```bash
gitt issue val vote-cancel-issue 1 "External solution found"
gitt i val cancel 42 "Issue invalid"
```

| Argument | Description |
|----------|-------------|
| `ISSUE_ID` | Issue to cancel |
| `REASON` | Reason for cancellation |

---

## Admin Commands (`gitt issue admin` / `gitt i a`)

Owner-only administrative commands.

### `gitt issue admin cancel`
Cancel an issue (owner only).

```bash
gitt issue admin cancel 1
gitt i a cancel 42
```

### `gitt issue admin payout`
Manual payout fallback (owner only).

```bash
gitt issue admin payout 1 5Hxxx...
gitt i a payout 42 <miner_coldkey>
```

| Argument | Description |
|----------|-------------|
| `COMPETITION_ID` | Completed competition |
| `MINER_COLDKEY` | Payout destination |

### `gitt issue admin set-owner`
Transfer contract ownership.

```bash
gitt issue admin set-owner 5Hnew...
```

### `gitt issue admin set-treasury`
Change treasury hotkey.

```bash
gitt issue admin set-treasury 5Hnew...
```

### `gitt issue admin set-validator`
Change validator hotkey.

```bash
gitt issue admin set-validator 5Hnew...
```

### `gitt issue admin set-config`
Update competition timing configuration.

```bash
gitt issue admin set-config --submission-window 1000 --deadline 5000
```

| Option | Description |
|--------|-------------|
| `--submission-window` | Submission window in blocks |
| `--deadline` | Competition deadline in blocks |
| `--proposal-expiry` | Proposal expiry in blocks |

---

## Full Command Reference Table

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

---

## Configuration

The CLI reads configuration from multiple sources (in priority order):

1. **CLI arguments** - Highest priority
2. **Environment variables**
   - `CONTRACT_ADDRESS` - Contract address
   - `WS_ENDPOINT` - WebSocket endpoint
   - `GITTENSOR_API_URL` - API URL
3. **Config file** (`~/.gittensor/contract_config.json`)
   ```json
   {
     "contract_address": "5Cxxx...",
     "ws_endpoint": "ws://localhost:9944",
     "api_url": "http://localhost:3000",
     "network": "local"
   }
   ```

---

## File Locations

| File | Description |
|------|-------------|
| `~/.gittensor/issue_preferences.json` | Local issue preferences |
| `~/.gittensor/contract_config.json` | Contract configuration |
| `~/.gittensor/cli_config.json` | CLI configuration (wallet, hotkey) |
