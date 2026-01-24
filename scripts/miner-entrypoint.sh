#!/bin/bash

if [ -z "$NETUID" ]; then echo "NETUID is not set" && exit 1; fi
if [ -z "$WALLET_NAME" ]; then echo "WALLET_NAME is not set" && exit 1; fi
if [ -z "$HOTKEY_NAME" ]; then echo "HOTKEY_NAME is not set" && exit 1; fi
if [ -z "$SUBTENSOR_NETWORK" ]; then echo "SUBTENSOR_NETWORK is not set" && exit 1; fi
if [ -z "$PORT" ]; then echo "PORT is not set" && exit 1; fi
if [ -z "$LOG_LEVEL" ]; then echo "LOG_LEVEL is not set" && exit 1; fi
if [ -z "$GITTENSOR_MINER_PAT" ]; then echo "GITTENSOR_MINER_PAT is not set" && exit 1; fi

exec python neurons/miner.py \
  --netuid ${NETUID} \
  --wallet.name ${WALLET_NAME} \
  --wallet.hotkey ${HOTKEY_NAME} \
  --subtensor.network ${SUBTENSOR_NETWORK} \
  --axon.port ${PORT} \
  --logging.${LOG_LEVEL} \
  "$@"
