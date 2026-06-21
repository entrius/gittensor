def _handle_storage_read(result: Optional[List[Issue]]) -> Dict:
    if result is None:
        return {
            "success": false,
            "error": {
                "type": "read_failed",
                "message": "Failed to read or decode issue storage"
            }
        }
    return {
        "success": true,
        "issue_count": len(result),
        "issues": result
    }