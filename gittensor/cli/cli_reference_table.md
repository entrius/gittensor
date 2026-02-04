# Gittensor CLI Reference Table (issues-v0)

## Command Reference

| CLI Command | Access | Tested | Testing Command | Notes |
|-------------|--------|--------|-----------------|-------|
| **Config (`gitt config`)** | | | | |
| `gitt config` | Read | [x] | `gitt config` | Show current config |
| `gitt config set <key> <value>` | Read | [x] | `gitt config set wallet alice` | Set config value |
| **View (`gitt view` / `gitt v`)** | | | | |
| `gitt view issues` | Read | [x] | `gitt v issues --rpc-url ws://localhost:9944 --contract <ADDR>` | |
| `gitt view issue <ID>` | Read | [x] | `gitt v issue 0 --rpc-url ws://localhost:9944 --contract <ADDR>` | |
| `gitt view issue-bounty-pool` | Read | [ ] | `gitt v issue-bounty-pool --rpc-url ws://localhost:9944 --contract <ADDR>` | This has bugs |
| `gitt view issue-pending-harvest` | Read | [ ] | `gitt v issue-pending-harvest --rpc-url ws://localhost:9944 --contract <ADDR>` | This has bugs |
| `gitt view issue-contract-config` | Read | [x] | `gitt v issue-contract-config --rpc-url ws://localhost:9944 --contract <ADDR>` | |
| **Register (`gitt register` / `gitt reg`)** | | | | |
| `gitt register issue` | Owner | [x] | `gitt reg issue --repo test/repo --issue 1 --bounty 10 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | This was also tested to ensure non-permitted wallets cannot reg issues. |
| **Harvest (`gitt harvest`)** | | | | |
| `gitt harvest` | Permissionless | [ ] | `gitt harvest --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice --verbose` | - The harvest functionality isn't filling in order of registration time, rather by size it seems? |
| **Validator (`gitt val`)** | | | | |
| `gitt val vote-issue-solution <ID> <HOTKEY> <COLDKEY> <PR>` | Validator | [ ] | `gitt val solution 0 <HOTKEY> <COLDKEY> 1 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name validator1` | alias: `solution` |
| `gitt val vote-issue-cancel <ID> <REASON>` | Validator | [ ] | `gitt val cancel 0 "reason" --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name validator1` | alias: `cancel` Successfully tested for: - Cancellation works for Registered issues - Cancellation works for Active issues - Cacellation fails for completed bounties - Cancelled bounty fails to be cancelled again |
| **Admin (`gitt admin` / `gitt a`)** | | | | |
| `gitt admin cancel-issue <ID>` | Owner | [ ] | `gitt a cancel-issue 0 --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | Not implemented |
| `gitt admin payout-issue <ID> <COLDKEY>` | Owner | [ ] | `gitt a payout-issue 0 <COLDKEY> --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | Not implemented - still takes solver coldkey (old) |
| `gitt admin set-owner <NEW_OWNER>` | Owner | [ ] | `gitt a set-owner <ADDR> --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | Not implemented |
| `gitt admin set-treasury <NEW_TREASURY>` | Owner | [ ] | `gitt a set-treasury <ADDR> --rpc-url ws://localhost:9944 --contract <ADDR> --wallet-name alice` | Not implemented |
