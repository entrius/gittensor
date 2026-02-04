# Gittensor CLI Reference Table (issues-v0)

## Quick Reference

**CLI Version:** 3.2.0
**Branch:** issues-v0

### Command Structure
```
gitt
├── config                             # CLI config management
│   ├── (default)                      # Show current config
│   └── set <key> <value>              # Set config value
├── view (alias: v)                    # Read commands
│   ├── issues                         # List all issues
│   ├── issue <ID>                     # View single issue
│   ├── issue-bounty-pool              # Alpha pool balance
│   ├── issue-pending-harvest          # Treasury stake
│   └── issue-contract-config          # Contract config (owner, treasury, netuid)
├── register (alias: reg)              # Registration commands
│   └── issue                          # Register new issue bounty
├── harvest                            # Harvest emissions (top-level, no subgroup)
├── val                                # Validator commands
│   ├── vote-issue-solution (alias: solution)
│   └── vote-issue-cancel (alias: cancel)
└── admin (alias: a)                   # Owner commands
    ├── cancel-issue
    ├── payout-issue
    ├── set-owner
    └── set-treasury
```

### Aliases
| Full Command | Alias |
|--------------|-------|
| `gitt view` | `gitt v` |
| `gitt register` | `gitt reg` |
| `gitt admin` | `gitt a` |
| `gitt val vote-issue-solution` | `gitt val solution` |
| `gitt val vote-issue-cancel` | `gitt val cancel` |

---

## Configuration

**Config File:** `~/.gittensor/config.json`

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

**Priority:** CLI flags > config file > defaults

---

## Command Reference Table

### CONFIG

| Command | Description | Full Command |
|---------|-------------|--------------|
| `gitt config` | Show current config | `gitt config` |
| `gitt config set` | Set a config value | `gitt config set <key> <value>` |

---

### VIEW Commands (`gitt view` / `gitt v`)

| Command | Source | Access | Full Command |
|---------|--------|--------|--------------|
| `view issues` | Contract | Read | `gitt v issues --rpc-url <URL> --contract <ADDR> --testnet --from-api --verbose` |
| `view issue <ID>` | Contract | Read | `gitt v issue <ISSUE_ID> --rpc-url <URL> --contract <ADDR> --verbose` |
| `view issue-bounty-pool` | Contract | Read | `gitt v issue-bounty-pool --rpc-url <URL> --contract <ADDR> --verbose` |
| `view issue-pending-harvest` | Contract | Read | `gitt v issue-pending-harvest --rpc-url <URL> --contract <ADDR> --verbose` |
| `view issue-contract-config` | Contract | Read | `gitt v issue-contract-config --rpc-url <URL> --contract <ADDR> --verbose` |

---

### REGISTER Commands (`gitt register` / `gitt reg`)

| Command | Source | Access | Full Command |
|---------|--------|--------|--------------|
| `register issue` | Contract | Owner | `gitt reg issue --repo <OWNER/REPO> --issue <NUM> --bounty <AMOUNT> --rpc-url <URL> --contract <ADDR> --testnet --wallet-name <NAME> --wallet-hotkey <KEY>` |

---

### HARVEST Command (`gitt harvest`)

| Command | Source | Access | Full Command |
|---------|--------|--------|--------------|
| `harvest` | Contract | Permissionless | `gitt harvest --rpc-url <URL> --contract <ADDR> --wallet-name <NAME> --wallet-hotkey <KEY> --verbose` |

---

### VALIDATOR Commands (`gitt val`)

| Command | Source | Access | Full Command |
|---------|--------|--------|--------------|
| `vote-issue-solution` | Contract | Validator | `gitt val vote-issue-solution <ISSUE_ID> <SOLVER_HOTKEY> <SOLVER_COLDKEY> <PR_NUM_OR_URL> --rpc-url <URL> --contract <ADDR> --wallet-name <NAME> --wallet-hotkey <KEY>` |
| `vote-issue-cancel` | Contract | Validator | `gitt val vote-issue-cancel <ISSUE_ID> "<REASON>" --rpc-url <URL> --contract <ADDR> --wallet-name <NAME> --wallet-hotkey <KEY>` |

---

### ADMIN Commands (`gitt admin` / `gitt a`)

| Command | Source | Access | Full Command |
|---------|--------|--------|--------------|
| `cancel-issue` | Contract | Owner | `gitt a cancel-issue <ISSUE_ID> --rpc-url <URL> --contract <ADDR> --wallet-name <NAME> --wallet-hotkey <KEY>` |
| `payout-issue` | Contract | Owner | `gitt a payout-issue <ISSUE_ID> <SOLVER_COLDKEY> --rpc-url <URL> --contract <ADDR> --wallet-name <NAME> --wallet-hotkey <KEY>` |
| `set-owner` | Contract | Owner | `gitt a set-owner <NEW_OWNER> --rpc-url <URL> --contract <ADDR> --wallet-name <NAME> --wallet-hotkey <KEY>` |
| `set-treasury` | Contract | Owner | `gitt a set-treasury <NEW_TREASURY> --rpc-url <URL> --contract <ADDR> --wallet-name <NAME> --wallet-hotkey <KEY>` |

---

## Detailed Command Reference

### `gitt config`
Show current CLI configuration.
```bash
gitt config
```

---

### `gitt config set`
Set a configuration value.

| Arg | Required | Description |
|-----|----------|-------------|
| `<key>` | Yes | Config key (wallet, hotkey, contract_address, ws_endpoint, api_url, network) |
| `<value>` | Yes | Value to set |

```bash
gitt config set wallet alice
gitt config set contract_address 5Cxxx...
gitt config set network local
```

---

### `gitt register issue`
Register a new issue with a bounty (OWNER ONLY).

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--repo` | Yes | - | Repository in owner/repo format |
| `--issue` | Yes | - | GitHub issue number |
| `--bounty` | Yes | - | Bounty amount in ALPHA tokens |
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--testnet` | No | false | Use testnet contract |
| `--wallet-name` | No | `default` | Wallet name |
| `--wallet-hotkey` | No | `default` | Hotkey name |

```bash
# Local dev
gitt register issue --repo opentensor/btcli --issue 144 --bounty 100 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice

# Testnet
gitt reg issue --repo opentensor/btcli --issue 144 --bounty 100 --testnet
```

---

### `gitt harvest`
Manually trigger emission harvest from contract treasury.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--wallet-name` | No | `validator` | Wallet name |
| `--wallet-hotkey` | No | `default` | Hotkey name |
| `--verbose` / `-v` | No | false | Show detailed output |

```bash
# Local dev
gitt harvest --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice --verbose

# Mainnet
gitt harvest
```

---

### `gitt view issues`
List available issues with status and bounty amounts.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--testnet` | No | false | Use testnet contract |
| `--from-api` | No | false | Force read from API |
| `--verbose` / `-v` | No | false | Show debug output |

```bash
# Local dev
gitt v issues --rpc-url ws://localhost:9944 --contract <ADDR> --verbose

# Testnet
gitt v issues --testnet
```

---

### `gitt view issue-bounty-pool`
View current alpha pool balance.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--verbose` / `-v` | No | false | Show debug output |

```bash
gitt v issue-bounty-pool --rpc-url ws://localhost:9944 --contract <ADDR>
```

---

### `gitt view issue-pending-harvest`
View pending emissions value (stake on treasury).

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--verbose` / `-v` | No | false | Show debug output |

```bash
gitt v issue-pending-harvest --rpc-url ws://localhost:9944 --contract <ADDR>
```

---

### `gitt view issue <ID>`
View raw issue data from contract.

| Arg/Flag | Required | Default | Description |
|----------|----------|---------|-------------|
| `<ISSUE_ID>` | Yes | - | Issue ID to view |
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--verbose` / `-v` | No | false | Show debug output |

```bash
gitt v issue 1 --rpc-url ws://localhost:9944 --contract <ADDR>
```

---

### `gitt view issue-contract-config`
View contract configuration.

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--verbose` / `-v` | No | false | Show debug output |

```bash
gitt v issue-contract-config --rpc-url ws://localhost:9944 --contract <ADDR>
```

---

### `gitt val vote-issue-solution`
Vote for a solution on an active issue (triggers auto-payout on consensus).

| Arg/Flag | Required | Default | Description |
|----------|----------|---------|-------------|
| `<ISSUE_ID>` | Yes | - | Issue to vote on |
| `<SOLVER_HOTKEY>` | Yes | - | Solver's hotkey address |
| `<SOLVER_COLDKEY>` | Yes | - | Solver's coldkey (payout destination) |
| `<PR_NUM_OR_URL>` | Yes | - | PR number or full GitHub URL |
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--wallet-name` | No | `default` | Wallet name |
| `--wallet-hotkey` | No | `default` | Hotkey name |

```bash
# With PR number
gitt val vote-issue-solution 1 5Hxxx... 5Hyyy... 123 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name validator1

# With PR URL
gitt val vote-issue-solution 1 5Hxxx... 5Hyyy... https://github.com/org/repo/pull/123 --wallet-name validator1

# Using alias
gitt val solution 1 5Hxxx... 5Hyyy... 123 --wallet-name validator1
```

---

### `gitt val vote-issue-cancel`
Vote to cancel an issue (Registered or Active status).

| Arg/Flag | Required | Default | Description |
|----------|----------|---------|-------------|
| `<ISSUE_ID>` | Yes | - | Issue to cancel |
| `<REASON>` | Yes | - | Reason for cancellation |
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--wallet-name` | No | `default` | Wallet name |
| `--wallet-hotkey` | No | `default` | Hotkey name |

```bash
gitt val vote-issue-cancel 1 "External solution found" --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name validator1

# Using alias
gitt val cancel 1 "Issue invalid" --wallet-name validator1
```

---

### `gitt admin cancel-issue`
Cancel an issue (OWNER ONLY). Returns bounty to pool.

| Arg/Flag | Required | Default | Description |
|----------|----------|---------|-------------|
| `<ISSUE_ID>` | Yes | - | Issue to cancel |
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--wallet-name` | No | `default` | Wallet name (must be owner) |
| `--wallet-hotkey` | No | `default` | Hotkey name |

```bash
gitt a cancel-issue 1 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice
```

---

### `gitt admin payout-issue`
Manual payout fallback (OWNER ONLY).

| Arg/Flag | Required | Default | Description |
|----------|----------|---------|-------------|
| `<ISSUE_ID>` | Yes | - | Completed issue ID |
| `<SOLVER_COLDKEY>` | Yes | - | Payout destination address |
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--wallet-name` | No | `default` | Wallet name (must be owner) |
| `--wallet-hotkey` | No | `default` | Hotkey name |

```bash
gitt a payout-issue 1 5Hyyy... --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice
```

---

### `gitt admin set-owner`
Transfer contract ownership (OWNER ONLY).

| Arg/Flag | Required | Default | Description |
|----------|----------|---------|-------------|
| `<NEW_OWNER>` | Yes | - | New owner address |
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--wallet-name` | No | `default` | Wallet name (must be owner) |
| `--wallet-hotkey` | No | `default` | Hotkey name |

```bash
gitt a set-owner 5Hnew... --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice
```

---

### `gitt admin set-treasury`
Change treasury hotkey (OWNER ONLY).

| Arg/Flag | Required | Default | Description |
|----------|----------|---------|-------------|
| `<NEW_TREASURY>` | Yes | - | New treasury hotkey address |
| `--rpc-url` | No | `wss://entrypoint-finney.opentensor.ai:443` | Subtensor RPC endpoint |
| `--contract` | No | config/default | Contract address |
| `--wallet-name` | No | `default` | Wallet name (must be owner) |
| `--wallet-hotkey` | No | `default` | Hotkey name |

```bash
gitt a set-treasury 5Htreasury... --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice
```

---

## Local Development Quick Commands

**Common local dev flags:**
```bash
--rpc-url ws://localhost:9944 --contract <CONTRACT_ADDR>
```

**Quick test sequence:**
```bash
# 1. Check CLI config
gitt config

# 2. Set config values
gitt config set wallet alice
gitt config set contract_address <ADDR>

# 3. View contract config
gitt v issue-contract-config --rpc-url ws://localhost:9944 --contract <ADDR>

# 4. Check bounty pool
gitt v issue-bounty-pool --rpc-url ws://localhost:9944 --contract <ADDR>

# 5. Register an issue (as owner/alice)
gitt register issue --repo test/repo --issue 1 --bounty 10 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice

# 6. List issues
gitt v issues --rpc-url ws://localhost:9944 --contract <ADDR>

# 7. Vote solution (as validator)
gitt val vote-issue-solution 0 <SOLVER_HOTKEY> <SOLVER_COLDKEY> 1 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name validator1

# 8. Harvest emissions
gitt harvest --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice --verbose
```

---

## Access Levels

| Level | Description |
|-------|-------------|
| **Owner** | Contract owner only |
| **Validator** | Registered validators |
| **Permissionless** | Anyone can call |
| **Read** | No auth required |

---

## Status Codes

| Status | Value | Description |
|--------|-------|-------------|
| Registered | 0 | Issue registered, awaiting activation |
| Active | 1 | Issue active, accepting solutions |
| Completed | 2 | Issue solved and paid out |
| Cancelled | 3 | Issue cancelled, bounty returned |
