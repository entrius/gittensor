#!/usr/bin/env python3
"""
Review Response Template — untuk menjawab review/CHANGES_REQUESTED dari maintainer.
Review Quality factor: max(0, 1.0 - 0.15 * CR_count) — setiap CHANGES_REQUESTED turunkan 0.15x.
Goal: 0 CHANGES_REQUESTED, atau minimal 1 round saja.

Usage:
  python3 review_response.py --list              # List available templates
  python3 review_response.py --type clarification
  python3 review_response.py --type change --commit-hash abc1234 --changes "renamed var, added guard"
  python3 review_response.py --pr 1129 --type fix-request --body "Change X to Y"
"""

import argparse, datetime

TEMPLATES = {
    "clarification": {
        "title": "Clarification requested by reviewer",
        "body": """Thanks for the review, @{REVIEWER}.

Regarding {POINT}:

{EXPLANATION}

This is explained in {SECTION} of the PR description. Let me know if you'd like me to expand that section with more detail.

No code changes needed — purely clarification.""",
        "when": "When reviewer asks 'why did you do X?' and the answer is already in the PR body.",
        "scoring_impact": "Neutral — no extra CHANGES_REQUESTED, just discussion.",
    },
    "change-accepted": {
        "title": "Change requested — applied",
        "body": """Thanks for the review, @{REVIEWER}.

Applied the suggested change:

{CHANGES}

Commit: `{COMMIT_HASH}`

The PR body has been updated to reflect this.""",
        "when": "Reviewer asks for a specific change that makes sense. Apply it, push, and use this response.",
        "scoring_impact": "1 CHANGES_REQUESTED → Review Quality multiplier drops by 0.15x. Acceptable if limited to 1 round.",
    },
    "alternative-suggestion": {
        "title": "Alternative approach proposed",
        "body": """Thanks for the suggestion, @{REVIEWER}.

I considered {ALTERNATIVE} during implementation, but chose {CURRENT} because:

{REASON}

That said, {CONCESSION} — happy to adjust if you feel strongly about it.

No changes pushed yet — waiting for your direction.""",
        "when": "Reviewer proposes a different approach. Respectfully explain why current approach is better, but stay open.",
        "scoring_impact": "Could trigger follow-up CR if reviewer disagrees. Use sparingly.",
    },
    "self-fix-noted": {
        "title": "Self-caught issue — fixed",
        "body": """Good catch — I noticed this too during {CONTEXT}.

Fixed in `{COMMIT_HASH}`:

{FIX_DESCRIPTION}

Tests still pass: {TEST_RESULT}""",
        "when": "Reviewer points out something you already fixed (self-before-review). Shows thoroughness.",
        "scoring_impact": "Neutral or positive — shows reviewer you're thorough.",
    },
    "scope-suggestion": {
        "title": "Out of scope — will follow up",
        "body": """Good idea, @{REVIEWER}. This is slightly out of scope for the current fix ({CURRENT_SCOPE}).

I've opened a follow-up issue: #{FOLLOWUP_ISSUE}

Or I can address it in a separate PR if you'd prefer. Let me know.""",
        "when": "Reviewer asks for something beyond the PR scope. Offer follow-up instead of scope creep.",
        "scoring_impact": "Neutral — keeps PR focused, shows organization.",
    },
    "simple-fix": {
        "title": "Trivial fix — applied",
        "body": """Done. Pushed as `{COMMIT_HASH}`.

Changes: {CHANGES}""",
        "when": "Reviewer asks for a trivial one-line change (typo, rename, etc.). Short response is fine.",
        "scoring_impact": "Minimal — 1 CR but resolved quickly.",
    },
}

def list_templates():
    print("\n=== Review Response Templates ===\n")
    for name, t in sorted(TEMPLATES.items()):
        print(f"  [{name}]")
        print(f"    When: {t['when']}")
        print(f"    Scoring: {t['scoring_impact']}")
        print()

def fill_template(name: str, **kwargs) -> str:
    if name not in TEMPLATES:
        print(f"❌ Unknown template: {name}")
        print(f"   Available: {', '.join(TEMPLATES.keys())}")
        return ""
    body = TEMPLATES[name]["body"]
    # Fill in defaults for missing kwargs
    defaults = {
        "REVIEWER": "reviewer",
        "POINT": "the specific point",
        "EXPLANATION": "[explanation]",
        "SECTION": "[section]",
        "CHANGES": "[list of changes]",
        "COMMIT_HASH": "[commit hash]",
        "ALTERNATIVE": "[alternative approach]",
        "CURRENT": "[current approach]",
        "REASON": "[reasoning]",
        "CONCESSION": "[concession if any]",
        "CONTEXT": "[context]",
        "FIX_DESCRIPTION": "[fix description]",
        "TEST_RESULT": "[test result]",
        "CURRENT_SCOPE": "[current scope]",
        "FOLLOWUP_ISSUE": "[issue number]",
    }
    for k, v in defaults.items():
        if k not in kwargs:
            kwargs[k] = v
    try:
        return body.format(**kwargs)
    except KeyError as e:
        return f"ERROR: Missing template variable {e}"

def main():
    p = argparse.ArgumentParser(description="Review response template generator")
    p.add_argument("--list", action="store_true", help="List available templates")
    p.add_argument("--type", default="", help="Template name")
    p.add_argument("--reviewer", default="anderdc", help="Reviewer GitHub handle")
    p.add_argument("--point", default="", help="Specific point being asked")
    p.add_argument("--explanation", default="", help="Your explanation")
    p.add_argument("--section", default="Root Cause / Impact", help="Section in PR body")
    p.add_argument("--changes", default="", help="What was changed")
    p.add_argument("--commit-hash", default="", help="Commit hash for the fix")
    p.add_argument("--test-result", default="", help="Test results after fix")
    p.add_argument("--context", default="testing", help="Context for self-fix")
    p.add_argument("--fix-description", default="", help="Description of fix")
    p.add_argument("--alternative", default="", help="Alternative approach considered")
    p.add_argument("--current", default="", help="Current approach chosen")
    p.add_argument("--reason", default="", help="Why current approach is better")
    p.add_argument("--concession", default="", help="Concession if any")
    p.add_argument("--current-scope", default="", help="Current PR scope")
    p.add_argument("--followup-issue", default="", help="Follow-up issue number")
    args = p.parse_args()

    if args.list:
        list_templates()
        return

    if not args.type:
        p.print_help()
        return

    kwargs = {
        "REVIEWER": args.reviewer,
        "POINT": args.point,
        "EXPLANATION": args.explanation,
        "SECTION": args.section,
        "CHANGES": args.changes,
        "COMMIT_HASH": args.commit_hash,
        "ALTERNATIVE": args.alternative,
        "CURRENT": args.current,
        "REASON": args.reason,
        "CONCESSION": args.concession,
        "CONTEXT": args.context,
        "FIX_DESCRIPTION": args.fix_description,
        "TEST_RESULT": args.test_result,
        "CURRENT_SCOPE": args.current_scope,
        "FOLLOWUP_ISSUE": args.followup_issue,
    }

    body = fill_template(args.type, **kwargs)
    if body:
        title = TEMPLATES[args.type]["title"]
        print(f"\n=== {title} ===\n")
        print(body)
        print()

if __name__ == "__main__":
    main()
