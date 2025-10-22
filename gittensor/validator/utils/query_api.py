import time
from typing import Any, Dict, Optional

import bittensor as bt
import requests

from gittensor.validator.utils.config import SERVICE_URL


def _make_service_request(endpoint: str, max_retries: int = 3) -> Optional[list]:
    """
    Internal helper to make HTTP requests to the service with retry logic.

    Args:
        endpoint: Full URL endpoint to query
        max_retries: Maximum number of retry attempts (default: 3)

    Returns:
        List of items from the service response, or None on error.
    """
    retry_count = 0

    while retry_count < max_retries:
        try:
            bt.logging.debug(f"Querying service at {endpoint} (attempt {retry_count + 1}/{max_retries})")

            response = requests.get(endpoint, timeout=30)
            response.raise_for_status()

            data = response.json()

            # Validate response is a list
            if not isinstance(data, list):
                bt.logging.error(f"Expected list response from service, got {type(data)}")
                return None

            return data

        except requests.exceptions.RequestException as e:
            retry_count += 1
            bt.logging.error(f"Service request failed (attempt {retry_count}/{max_retries}): {e}")

            if retry_count < max_retries:
                bt.logging.debug(f"Retrying in 30 seconds...")
                time.sleep(30)
            else:
                bt.logging.error(f"Max retries ({max_retries}) reached, failed to get a response from the service...")
                return None

        except Exception as e:
            bt.logging.error(f"Unexpected error while querying service: {e}")
            return None

    return None


def query_master_repo_list(max_retries: int = 3) -> Dict[str, Dict[str, Any]]:
    """
    Query the external service to retrieve repository data including weights and inactive status.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)

    Returns:
        Dictionary mapping fullName (str) to repository data dict containing:
            - weight (float): Repository weight
            - inactiveAt (str | None): Timestamp string when repo became inactive, or None if active
        Returns empty dict if SERVICE_URL is not set or on error.
    """
    if not SERVICE_URL:
        bt.logging.error("SERVICE_URL is not set")
        return {}

    endpoint = f'{SERVICE_URL}/dash/repos'
    data = _make_service_request(endpoint, max_retries)

    if data is None:
        return {}

    result = {}
    for item in data:
        if not isinstance(item, dict):
            bt.logging.warning(f"Skipping non-dict item in response: {item}")
            continue

        full_name = item.get('fullName')
        weight = item.get('weight')
        inactive_at = item.get('inactiveAt')

        if full_name is None or weight is None:
            bt.logging.warning(f"Skipping item with missing fullName or weight: {item}")
            continue

        try:
            weight_float = float(weight)
            result[full_name] = {'weight': weight_float, 'inactiveAt': inactive_at}  # Can be None or timestamp string
        except (ValueError, TypeError) as e:
            bt.logging.warning(f"Could not convert weight to float for {full_name}: {weight} - {e}")
            continue

    bt.logging.debug(f"Successfully retrieved {len(result)} repository entries from service")
    return result


def query_master_programming_language_list(max_retries: int = 3) -> Dict[str, float]:
    """
    Query the external service to retrieve programming language weights.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)

    Returns:
        Dictionary mapping extension (str) to weight (float).
        Returns empty dict if SERVICE_URL is not set or on error.
    """
    if not SERVICE_URL:
        bt.logging.error("SERVICE_URL is not set")
        return {}

    endpoint = f'{SERVICE_URL}/dash/languages'
    data = _make_service_request(endpoint, max_retries)

    if data is None:
        return {}

    result = {}
    for item in data:
        if not isinstance(item, dict):
            bt.logging.warning(f"Skipping non-dict item in response: {item}")
            continue

        extension = item.get('extension')
        weight = item.get('weight')

        if extension is None or weight is None:
            bt.logging.warning(f"Skipping item with missing extension or weight: {item}")
            continue

        try:
            weight_float = float(weight)
            result[extension] = weight_float
        except (ValueError, TypeError) as e:
            bt.logging.warning(f"Could not convert weight to float for {extension}: {weight} - {e}")
            continue

    bt.logging.debug(f"Successfully retrieved {len(result)} language entries from service")
    return result
