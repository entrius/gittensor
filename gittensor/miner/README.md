## Miner Guide

Miners

- Provide a generated Github PAT to verify account ownership
- Work on PRs for recognized repositories
- Receive incentive for successfully merged PRs

Running a miner requires deployment on a server with reliable, continuous availability.  
Validators regularly query miners to verify their GitHub PAT, and miners must remain responsive to these requests.

_MINIMUM REQUIREMENTS: python 3.11+, 2 CPUs, 4GB RAM_

You can run a miner following the **Autoupdater Setup** or **Manual Setup**

---

### 1. Ensure that you have an accessible coldkey and hotkey.

Please refer to the [official Bittensor documentation](https://docs.learnbittensor.org/keys/working-with-keys) for creating or importing a Bittensor wallet.

### 2. Register to the subnet

- To register a miner:

```
# testnet
btcli subnet register --netuid 422 \
--wallet-name WALLET_NAME \
--hotkey WALLET_HOTKEY \
--network test

# mainnet
btcli subnet register --netuid 74 \
--wallet-name WALLET_NAME \
--hotkey WALLET_HOTKEY \
```

### 3. Create a Github fine-grained PAT

- In Github go to your settings, find `developer settings`

- In `developer settings`, go to `Personal access tokens`, then `Fine-grained tokens`

- Then click `Generate new token`

- token name
  `gittensor`

- Expiration
  `No Expiration`

_NOTE: you can provide an expiration if you'd like, but be sure to refresh/create a new one for your miner once it expires_

- Repository access
  `Public repositories`

- Permissions
  `Events`
  - access
    `Read only`

- Hit `Generate token`

- Copy generated token for next step

### 4. Now setup your miner and get it running by either using the [Autoupdater Setup (Recommended)](#autoupdater-setup-recommended) or the [Manual Setup](#manual-setup).

### Autoupdater Setup (Recommended)

#### A. Clone the repository

- Clone the gittensor repo

```bash
git clone git@github.com:entrius/gittensor.git
```

- Step into your freshly cloned gittensor folder

```bash
cd gittensor
```

#### B. Setup environment

- Create a `.env` file in `gittensor/miner` folder using `.env.example` as a guide, then fill with your own env variables

```bash
cp gittensor/miner/.env.example gittensor/miner/.env
```

- Update your copied `.env` file with your GitHub PAT, subnet netuid, network, and wallet details:

```bash
mypat="<your_github_pat_here>"
netuid=422                # Replace with <MainNetUID> for mainnet
network="test"            # Replace with "finney" for mainnet
coldkey="<your_coldkey_wallet_name>"
hotkey="<your_hotkey_name>"

sed -i \
  -e "s|^GITTENSOR_MINER_PAT=.*|GITTENSOR_MINER_PAT=$mypat|" \
  -e "s|^NETUID=.*|NETUID=$netuid|" \
  -e "s|^SUBTENSOR_NETWORK=.*|SUBTENSOR_NETWORK=$network|" \
  -e "s|^WALLET_NAME=.*|WALLET_NAME=$coldkey|" \
  -e "s|^HOTKEY_NAME=.*|HOTKEY_NAME=$hotkey|" \
  gittensor/miner/.env
```

- Make scripts executable

```bash
sudo chmod +x scripts/*
```

- Run the `setup_env` script

```bash
# make sure you're at the root of the project
./scripts/setup_env.sh
```

#### C. Run miner

This script will start a miner in a PM2 process

```bash
# make sure you're at the root of the project
./scripts/run_miner.sh
```

#### D. Run autoupdater (runs alongside miner with PM2)

The autoupdater runs in a PM2 process as well, alongside the miner

```bash
# make sure you're at the root of the project
./scripts/run_autoupdater.sh --processes miner --check-interval 900
```

**Done! You you are now running a gittensor miner**

---

##### Process Management & Monitoring

Now that your miner and/or autoupdater are running, you can use these PM2 commands to monitor and manage your processes:

###### Check Process Status

```bash
# Display process list with details
pm2 list

# Shows all running PM2 processes with status, CPU/memory usage, and uptime
pm2 status

# View logs for specific miner process
pm2 logs gt-miner

# View logs for autoupdater
pm2 logs gt-autoupdater

# Restart miner
pm2 restart gt-miner

# Clear logs for specific process
pm2 flush gt-miner
```

_For more PM2 commands and options, run `pm2 --help` or visit the [PM2 documentation](https://pm2.keymetrics.io/docs/usage/quick-start/)_

---

### Manual Setup

#### A. Setup environment

- Create venv + install dependencies

```bash
# make sure you're at the root of the project
python -m venv venv
source venv/bin/activate

pip install -e .
```

- Export your GitHub PAT as an environment variable

```bash
export GITTENSOR_MINER_PAT=your_github_token_here
```

#### B. Run the miner (manually).

_Note: You may want to run the miner in some background process that ensures it is consistently live to respond to validator queries_

```bash
# testnet
python neurons/miner.py --wallet.name <test-wallet-name> --wallet.hotkey <test-hotkey-name> --netuid 422 --axon.port 8098 --subtensor.network test --logging.debug --blacklist.min_stake 0

# mainnet
python neurons/miner.py --wallet.name <wallet> --wallet.hotkey <hotkey> --netuid 74 --axon.port 8098 --subtensor.network finney --logging.debug
```

---

### Work on recognized repositories

- see all recognized repositories [here](https://gittensor.io/repositories)

- Miners receive score once their pull request to a recognized repository becomes accepted and merged

_NOTE: Some Github organizations forbid access via fine-grained PAT if the token's lifetime is indefinite. Be aware if you are part of any organizations on Github and plan accordingly if that happens to be the case (by either leaving the organization or using a different account)._
