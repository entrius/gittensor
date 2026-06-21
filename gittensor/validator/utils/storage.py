def _read_issues_from_child_storage(child_storage_key: str) -> Optional[List[Issue]]:
    packed_bytes = _read_contract_packed_storage(child_storage_key)
    if packed_bytes is None:
        return None  # Propagate failure
    try:
        return decode_issues(packed_bytes)
    except Exception:
        return None  # Propagate decode failure