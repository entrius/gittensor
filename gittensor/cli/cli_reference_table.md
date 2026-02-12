# Gittensor CLI Reference Table (issues-v0)

## Global Options

All commands that interact with the network support:
- `--network` / `-n` : Network name (`finney`, `test`, `local`). Default: `finney`
- `--rpc-url` : Custom RPC endpoint (overrides `--network`)
- `--wallet-name` / `--wallet.name` / `--wallet` : Wallet name
- `--wallet-hotkey` / `--wallet.hotkey` / `--hotkey` : Hotkey name

## Command Reference

| CLI Command | Access | Tested | Testing Command | Notes |
|-------------|--------|--------|-----------------|-------|
| **Config (`gitt config`)** | | | | |
| `gitt config` | Read | [x] | `gitt config` | Show current config |
| `gitt config set <key> <value>` | Read | [x] | `gitt config set wallet alice` | Set config value |
| **Issues (`gitt issues` / `gitt i`)** | | | | |
| `gitt issues list` | Read | [x] | `gitt i list --network local --contract <ADDR>` | List all issues |
| `gitt issues list --id <ID>` | Read | [x] | `gitt i list --id 0 --network local --contract <ADDR>` | View specific issue detail |
| `gitt issues register` | Owner | [x] | `gitt i register --repo test/repo --issue 1 --bounty 10 --network local --contract <ADDR> --wallet alice` | Also tested non-permitted wallets cannot register. |
| `gitt issues bounty-pool` | Read | [x] | `gitt i bounty-pool --network local --contract <ADDR>` | Sum of all issue bounty amounts |
| `gitt issues pending-harvest` | Read | [x] | `gitt i pending-harvest --network local --contract <ADDR>` | Treasury stake minus allocated bounties |
| **Harvest (`gitt harvest`)** | | | | |
| `gitt harvest` | Permissionless | [x] | `gitt harvest --network local --contract <ADDR> --wallet alice --verbose` | Fixed: now fills in FIFO order |
| **Vote (`gitt vote`)** | | | | |
| `gitt vote solution <ID> <HOTKEY> <COLDKEY> <PR>` | Validator | [x] | `gitt vote solution 0 <HOTKEY> <COLDKEY> 1 --network local --contract <ADDR> --wallet validator1` | Triggers auto-payout on consensus |
| `gitt vote cancel <ID> <REASON>` | Validator | [x] | `gitt vote cancel 0 "reason" --network local --contract <ADDR> --wallet validator1` | Tested: works for Registered/Active, fails for Completed/Cancelled. |
| **Admin (`gitt admin` / `gitt a`)** | | | | |
| `gitt admin info` | Read | [x] | `gitt a info --network local --contract <ADDR>` | View contract config (owner, treasury, netuid) |
| `gitt admin cancel-issue <ID>` | Owner | [x] | `gitt a cancel-issue 0 --network local --contract <ADDR> --wallet alice` | Not implemented |
| `gitt admin payout-issue <ID>` | Owner | [x] | `gitt a payout-issue 0 --network local --contract <ADDR> --wallet alice` | Not implemented - solver determined by validator consensus |
| `gitt admin set-owner <NEW_OWNER>` | Owner | [x] | `gitt a set-owner <ADDR> --network local --contract <ADDR> --wallet alice` | |
| `gitt admin set-treasury <NEW_TREASURY>` | Owner | [x] | `gitt a set-treasury <ADDR> --network local --contract <ADDR> --wallet alice` | |

## Network Shortcuts

| `--network` | RPC Endpoint |
|-------------|-------------|
| `finney` (default) | `wss://entrypoint-finney.opentensor.ai:443` |
| `test` | `wss://test.finney.opentensor.ai:443` |
| `local` | `ws://127.0.0.1:9944` |
