#!/bin/bash
# Entrypoint for the serving miner neuron (sub-subnet B beta).

if [ -z "$NETUID" ]; then echo "NETUID is required" && exit 1; fi
if [ -z "$WALLET_NAME" ]; then echo "WALLET_NAME is required" && exit 1; fi
if [ -z "$HOTKEY_NAME" ]; then echo "HOTKEY_NAME is required" && exit 1; fi
if [ -z "$SUBTENSOR_NETWORK" ]; then echo "SUBTENSOR_NETWORK is required" && exit 1; fi
if [ -z "$PORT" ]; then echo "PORT is required" && exit 1; fi
if [ -z "$LOG_LEVEL" ]; then echo "LOG_LEVEL is required" && exit 1; fi

exec python neurons/serving_miner.py \
  --netuid ${NETUID} \
  --wallet.name ${WALLET_NAME} \
  --wallet.hotkey ${HOTKEY_NAME} \
  --subtensor.network ${SUBTENSOR_NETWORK} \
  --axon.port ${PORT} \
  --logging.${LOG_LEVEL} \
  "$@"
