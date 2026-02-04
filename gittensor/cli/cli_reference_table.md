# Gittensor CLI Reference Table (issues-v0)

## Command Reference

| CLI Command | Access | Tested | Testing Command | Notes |
|-------------|--------|--------|-----------------|-------|
| **Config (`gitt config`)** | | | | |
| `gitt config` | Read | [ ] | `gitt config` | Show current config |
| `gitt config set <key> <value>` | Read | [ ] | `gitt config set wallet alice` | Set config value |
| **View (`gitt view` / `gitt v`)** | | | | |
| `gitt view issues` | Read | [ ] | `gitt v issues --rpc-url ws://localhost:9944 --contract <ADDR>` | |
| `gitt view issue <ID>` | Read | [ ] | `gitt v issue 0 --rpc-url ws://localhost:9944 --contract <ADDR>` | |
| `gitt view issue-bounty-pool` | Read | [ ] | `gitt v issue-bounty-pool --rpc-url ws://localhost:9944 --contract <ADDR>` | |
| `gitt view issue-pending-harvest` | Read | [ ] | `gitt v issue-pending-harvest --rpc-url ws://localhost:9944 --contract <ADDR>` | |
| `gitt view issue-contract-config` | Read | [ ] | `gitt v issue-contract-config --rpc-url ws://localhost:9944 --contract <ADDR>` | |
| **Register (`gitt register` / `gitt reg`)** | | | | |
| `gitt register issue` | Owner | [ ] | `gitt reg issue --repo test/repo --issue 1 --bounty 10 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | |
| **Harvest (`gitt harvest`)** | | | | |
| `gitt harvest` | Permissionless | [ ] | `gitt harvest --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice --verbose` | |
| **Validator (`gitt val`)** | | | | |
| `gitt val vote-issue-solution <ID> <HOTKEY> <COLDKEY> <PR>` | Validator | [ ] | `gitt val solution 0 <HOTKEY> <COLDKEY> 1 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name validator1` | alias: `solution` |
| `gitt val vote-issue-cancel <ID> <REASON>` | Validator | [ ] | `gitt val cancel 0 "reason" --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name validator1` | alias: `cancel` |
| **Admin (`gitt admin` / `gitt a`)** | | | | |
| `gitt admin cancel-issue <ID>` | Owner | [ ] | `gitt a cancel-issue 0 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | |
| `gitt admin payout-issue <ID> <COLDKEY>` | Owner | [ ] | `gitt a payout-issue 0 <COLDKEY> --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | |
| `gitt admin set-owner <NEW_OWNER>` | Owner | [ ] | `gitt a set-owner <ADDR> --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | Not implemented |
| `gitt admin set-treasury <NEW_TREASURY>` | Owner | [ ] | `gitt a set-treasury <ADDR> --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | Not implemented |
