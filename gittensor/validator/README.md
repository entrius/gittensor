## Validator Guide

Validators

- Authenticate users via github PAT
- Retrieve github PR data
- Verify successful and meaningful merged PRs
- Calculate miner score based on incentive mechanism

Running a validator requires deployment on a server with reliable, continuous availability.  
Validators continuously query miners to verify their GitHub PATs and evaluate their contributions.

_MINIMUM REQUIREMENTS: python 3.11+, 4 CPUs, 8GB RAM_

You can run a validator following the **Autoupdater Setup** or **Manual Setup**

---

### 1. Ensure that you have an accessible coldkey and hotkey.

Please refer to the [official Bittensor documentation](https://docs.learnbittensor.org/keys/working-with-keys) for creating or importing a Bittensor wallet.

### 2. Register to the subnet

- To register a validator:

```
# testnet
btcli subnet register --netuid 422 \
--wallet-name WALLET_NAME \
--hotkey WALLET_HOTKEY \
--network test

# mainnet
btcli subnet register --netuid 74 \
--wallet-name WALLET_NAME \
--hotkey WALLET_HOTKEY
```

### 3. Now setup your validator and get it running by either using the [Autoupdater Setup (Recommended)](#autoupdater-setup-recommended) or the [Manual Setup](#manual-setup).

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

- Create a `.env` file in `gittensor/validator` folder using `.env.example` as a guide, then fill with your own env variables

```bash
cp gittensor/validator/.env.example gittensor/validator/.env
```

- Update your copied `.env` file with your subnet netuid, network, and wallet details:

```bash
netuid=422                # Replace with <MainNetUID> for mainnet
network="test"            # Replace with "finney" for mainnet
coldkey="<your_coldkey_wallet_name>"
hotkey="<your_hotkey_name>"

sed -i \
  -e "s|^NETUID=.*|NETUID=$netuid|" \
  -e "s|^SUBTENSOR_NETWORK=.*|SUBTENSOR_NETWORK=$network|" \
  -e "s|^WALLET_NAME=.*|WALLET_NAME=$coldkey|" \
  -e "s|^HOTKEY_NAME=.*|HOTKEY_NAME=$hotkey|" \
  gittensor/validator/.env
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

#### C. Run validator

This script will start a validator in a PM2 process

```bash
# make sure you're at the root of the project
./scripts/run_validator.sh
```

#### D. Run autoupdater (runs alongside validator with PM2)

The autoupdater runs in a PM2 process as well, alongside the validator

```bash
# make sure you're at the root of the project
./scripts/run_autoupdater.sh --processes validator --check-interval 120
```

**Done! You are now running a gittensor validator**

---

##### Process Management & Monitoring

Now that your validator and/or autoupdater are running, you can use these PM2 commands to monitor and manage your processes:

###### Check Process Status

```bash
# Display process list with details
pm2 list

# Shows all running PM2 processes with status, CPU/memory usage, and uptime
pm2 status

# View logs for specific validator process
pm2 logs gt-validator

# View logs for autoupdater
pm2 logs gt-autoupdater

# Restart validator
pm2 restart gt-validator

# Clear logs for specific process
pm2 flush gt-validator
```

_For more PM2 commands and options, run `pm2 --help` or visit the [PM2 documentation.] (https://pm2.keymetrics.io/docs/usage/quick-start/)_

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

- Log into wandb

```bash
export WANDB_API_KEY=<your_wandb_key>

wandb login $WANDB_API_KEY
```

#### B. Run the validator (manually)

_Note: You may want to run the validator in some background process that ensures it is consistently live to validate miners_

```bash
# testnet
python neurons/validator.py --wallet.name <test-wallet-name> --wallet.hotkey <test-hotkey-name> --netuid 422 --axon.port 8099 --subtensor.network test --logging.debug

# mainnet
python neurons/validator.py --wallet.name <wallet> --wallet.hotkey <hotkey> --netuid 74 --axon.port 8099 --subtensor.network finney --logging.debug
```
