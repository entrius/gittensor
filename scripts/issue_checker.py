#!/usr/bin/env python3
"""Cek issue sebelum bikin PR — optimasi Issue Bonus."""

import subprocess, sys, json

REPO = "entrius/gittensor"
MAINTAINER_ASSOCIATIONS = ["OWNER", "MEMBER", "COLLABORATOR"]

def check_issue(issue_num: int):
    r = subprocess.run(
        ["gh", "issue", "view", str(issue_num), "--repo", REPO,
         "--json", "number,title,state,author,labels",
         "--jq", "{number, title, state, login: .author.login, labels: [.labels[].name]}"],
        capture_output=True, text=True, timeout=15
    )
    if r.returncode != 0:
        print(f"❌ Issue #{issue_num} not found: {r.stderr}")
        return None
    result = json.loads(r.stdout)
    # Get authorAssociation via REST API
    import subprocess as sp
    r2 = sp.run(
        ["gh", "api", f"repos/{REPO}/issues/{issue_num}",
         "--jq", ".author_association"],
        capture_output=True, text=True, timeout=10
    )
    result["assoc"] = r2.stdout.strip()
    return result

def evaluate(issue: dict) -> dict:
    is_maintainer = issue["assoc"] in MAINTAINER_ASSOCIATIONS
    multiplier = 1.66 if is_maintainer else 1.33
    return {
        "multiplier": multiplier,
        "is_maintainer": is_maintainer,
        "author": issue["login"],
        "assoc": issue["assoc"],
        "state": issue["state"],
        "labels": issue["labels"],
    }

def print_report(issue_num: int, result: dict):
    print("=" * 60)
    print(f"  Issue #{issue_num}: {result.get('title', '')[:60]}")
    print("=" * 60)
    print(f"  Author: {result['author']} ({result['assoc']})")
    print(f"  State:  {result['state']}")
    print(f"  Labels: {', '.join(result['labels'])}")
    print(f"  {'─' * 58}")
    print(f"  Issue Bonus multiplier: {result['multiplier']}x")
    print(f"  Type: {'MAINTAINER (1.66x) 🏆' if result['is_maintainer'] else 'STANDARD (1.33x) ✅'}")
    print(f"  {'─' * 58}")
    if result['is_maintainer']:
        print(f"  ✅ Issue ini memberi Issue Bonus TERTINGGI (1.66x)")
    print()

def suggest_issues():
    """Cari open issues yang authored oleh maintainer."""
    # Gh issue list doesn't expose authorAssociation via JSON; fetch individually.
    r = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open",
         "--json", "number,title,author",
         "--limit", "30"],
        capture_output=True, text=True, timeout=15
    )
    if r.returncode != 0:
        print(f"Error: {r.stderr}"); return
    issues = json.loads(r.stdout)

    maintainer_issues = []
    for issue in issues[:20]:
        nr = issue["number"]
        r2 = subprocess.run(
            ["gh", "api", f"repos/{REPO}/issues/{nr}",
             "--jq", ".author_association"],
            capture_output=True, text=True, timeout=10
        )
        assoc = r2.stdout.strip()
        if assoc in ("MEMBER", "OWNER", "COLLABORATOR"):
            maintainer_issues.append({**issue, "assoc": assoc})

    if maintainer_issues:
        print(f"\n🔍 {len(maintainer_issues)} open issues dari MAINTAINER (bonus 1.66x):")
        for i in maintainer_issues[:10]:
            print(f"  #{i['number']:5} [{i['assoc']:<12}] {i['title'][:60]}")
    else:
        print("\n⚠️  Tidak ada open issues dari maintainer saat ini.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Gunakan:")
        print(f"  python3 {sys.argv[0]} <issue_number>  — cek satu issue")
        print(f"  python3 {sys.argv[0]} --suggest         — cari maintainer issues")
        sys.exit(1)

    if sys.argv[1] == "--suggest":
        suggest_issues()
    else:
        issue_num = int(sys.argv[1])
        issue = check_issue(issue_num)
        if issue:
            result = evaluate(issue)
            print_report(issue_num, {**issue, **result})
