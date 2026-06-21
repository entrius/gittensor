def find_prs_for_issue(issue_id):
    prs = _search_issue_referencing_prs_graphql(issue_id)
    return prs  # Changed from 'return prs or []' to 'return prs'

# Other existing code in the file remains unchanged.

# Example of other functions in the same file:

def _search_issue_referencing_prs_graphql(issue_id):
    # Simulated GraphQL lookup logic
    # Returns None on failure
    pass

# Additional functions and logic in the file...
