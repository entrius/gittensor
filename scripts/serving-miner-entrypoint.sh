#!/bin/bash
# Entrypoint for the serving miner neuron (sub-subnet B beta).
#
# The neuron only fronts a runtime; run the blessed runtime next to it first (one per GPU):
#   docker run -d --name sparkinfer --gpus all -p 8080:8080 -v sparkmodels:/opt/sparkinfer/models \
#     -e MODEL_SHA256=<model_sha256 from gittensor/validator/weights/serving_loadout.json> \
#     entrius/sparkinfer:<runtime_pin commit from the same file>
# then point this neuron at it (SERVING_RELEASE=<model_id>, base_url in the loadout). The image tag IS the
# release: validators verify against the same image, so any other build/quant fails audits.

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
