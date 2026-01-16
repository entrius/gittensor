# Docker Deployment Guide

This guide covers deploying the Gittensor validator using Docker with automatic updates via Watchtower.

## Quick Start

1. **Install Docker** (if not already installed)
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   # Log out and back in for group changes to take effect
   ```

2. **Clone the repository**
   ```bash
   git clone https://github.com/entrius/gittensor.git
   cd gittensor
   ```

3. **Configure environment**
   ```bash
   cp env.example .env
   nano .env  # or your preferred editor
   ```

4. **Start the validator**
   ```bash
   docker-compose up -d
   ```

That's it! Your validator is now running with automatic updates enabled.

## Configuration

### Required Settings

| Variable | Description |
|----------|-------------|
| `NETUID` | Bittensor network UID (74 for Gittensor) |
| `WALLET_NAME` | Your wallet name |
| `HOTKEY_NAME` | Your hotkey name |
| `WANDB_API_KEY` | Weights & Biases API key |

### Optional Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `SUBTENSOR_NETWORK` | `finney` | Network: finney, test, local |
| `PORT` | `8099` | Validator axon port |
| `LOG_LEVEL` | `info` | Logging: info, debug, trace |
| `STORE_DB_RESULTS` | `false` | Store results in database |
| `WALLET_PATH` | `~/.bittensor/wallets` | Host wallet directory |
| `DOCKER_IMAGE` | `entrius/gittensor-validator` | Docker image |
| `DOCKER_TAG` | `latest` | Image tag |

## How Auto-Updates Work

The deployment includes [Watchtower](https://containrrr.dev/watchtower/), which automatically monitors Docker Hub for new images.

1. When code is pushed to the `main` branch, GitHub Actions builds a new Docker image
2. The image is pushed to Docker Hub with tags: `latest`, `sha-<commit>`, `v<version>`
3. Watchtower polls Docker Hub every 5 minutes (configurable)
4. When a new image is detected, Watchtower:
   - Pulls the new image
   - Gracefully stops the running container
   - Starts a new container with the updated image
   - Removes the old image to save disk space

### Update Timeline

- Code push to main → Image available on Docker Hub: ~5 minutes
- Watchtower poll interval: 5 minutes
- **Maximum time to update: ~10 minutes**

## Management Commands

### View Logs
```bash
# Validator logs
docker-compose logs -f validator

# Watchtower logs
docker-compose logs -f watchtower

# Last 100 lines
docker-compose logs --tail=100 validator
```

### Service Control
```bash
# Stop all services
docker-compose down

# Restart validator
docker-compose restart validator

# Rebuild and restart (after local changes)
docker-compose up -d --build

# Pull latest image manually
docker-compose pull
```

### Health Check
```bash
# Check container status
docker-compose ps

# Check health status
docker inspect gittensor-validator --format='{{.State.Health.Status}}'

# View resource usage
docker stats gittensor-validator
```

## Rollback Procedure

If a new update causes issues, you can rollback to a previous version:

1. **Stop the validator**
   ```bash
   docker-compose down
   ```

2. **Edit `.env` to specify a previous tag**
   ```bash
   # Find available tags at: https://hub.docker.com/r/entrius/gittensor-validator/tags
   DOCKER_TAG=sha-abc1234  # Use a specific commit SHA
   # or
   DOCKER_TAG=v1.0.0  # Use a specific version
   ```

3. **Start with the pinned version**
   ```bash
   docker-compose up -d
   ```

4. **Temporarily disable Watchtower** (optional)
   ```bash
   docker-compose stop watchtower
   ```

5. **Re-enable after the issue is fixed**
   ```bash
   # Remove the DOCKER_TAG override or set back to 'latest'
   DOCKER_TAG=latest
   docker-compose up -d
   ```

## Database Connectivity

If you need to connect the validator to an existing Postgres container:

1. **Create a shared network** (if it doesn't exist)
   ```bash
   docker network create gittensor-network
   ```

2. **Connect your Postgres container to the network**
   ```bash
   docker network connect gittensor-network your-postgres-container
   ```

3. **Update `docker-compose.yml`** to use the external network:
   ```yaml
   networks:
     default:
       external: true
       name: gittensor-network
   ```

4. **Set database environment variables** in `.env`:
   ```bash
   STORE_DB_RESULTS=true
   DB_HOST=your-postgres-container  # Container name, not localhost
   DB_PORT=5432
   DB_NAME=gittensor
   DB_USER=gittensor
   DB_PASSWORD=your-password
   ```

## Troubleshooting

### Container won't start

1. Check logs for errors:
   ```bash
   docker-compose logs validator
   ```

2. Verify wallet mount:
   ```bash
   ls -la ~/.bittensor/wallets/
   ```

3. Check environment variables:
   ```bash
   docker-compose config
   ```

### Wallet not found

Ensure your wallet directory is correctly mounted:
```bash
# Check if wallet exists on host
ls ~/.bittensor/wallets/$WALLET_NAME/hotkeys/$HOTKEY_NAME

# Verify mount in container
docker exec gittensor-validator ls -la /home/validator/.bittensor/wallets/
```

### Watchtower not updating

1. Check Watchtower logs:
   ```bash
   docker-compose logs watchtower
   ```

2. Verify the label is set:
   ```bash
   docker inspect gittensor-validator --format='{{.Config.Labels}}'
   ```

3. Manual pull test:
   ```bash
   docker pull entrius/gittensor-validator:latest
   ```

### Out of memory

Increase memory limits in `docker-compose.yml`:
```yaml
deploy:
  resources:
    limits:
      memory: 8G  # Increase from 4G
    reservations:
      memory: 4G  # Increase from 2G
```

### Port already in use

Change the port mapping in `.env`:
```bash
PORT=8100  # Or another available port
```

## Security Considerations

- **Wallet mount is read-only**: The validator cannot modify your wallet files
- **Non-root user**: Container runs as UID 1000 for security
- **No shell access by default**: Use `docker exec -it gittensor-validator /bin/bash` for debugging
- **Secrets via environment**: Never commit `.env` files to version control

## Resource Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 2 cores | 4 cores |
| Memory | 2 GB | 4 GB |
| Disk | 10 GB | 20 GB |
| Network | Stable connection | Low latency |
