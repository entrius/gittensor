prs = _search_issue_referencing_prs_graphql(...)
return prs  # ← returns None on failure, preserving error state