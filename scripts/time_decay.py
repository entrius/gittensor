import json
from datetime import datetime, timezone
from math import exp

PR_OPEN_PATH = "/data/user/0/gptos.intelligence.assistant/cache/opencode/pr_open_raw.json"

DECAY_K = 0.0161
DECAY_T0 = 297.4
GRACE_HOURS = 12

def time_decay_multiplier(hours: float) -> float:
    if hours <= GRACE_HOURS:
        return 1.0
    return 1.0 / (1.0 + exp(DECAY_K * (hours - DECAY_T0)))

def categorize(hours: float, mult: float) -> str:
    if mult >= 0.80:
        return "FRESH"
    elif mult >= 0.50:
        return "AGING"
    elif mult >= 0.20:
        return "STALE"
    return "EXPIRED"

def analyze(prs: list) -> list:
    now = datetime.now(timezone.utc)
    results = []
    for pr in sorted(prs, key=lambda x: x["createdAt"]):
        created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
        hours = (now - created).total_seconds() / 3600
        mult = time_decay_multiplier(hours)
        cat = categorize(hours, mult)
        results.append({**pr, "hours": hours, "mult": mult, "cat": cat})
    return results

def print_report(results: list):
    print("=" * 74)
    print("              TIME DECAY TRACKER — GITTENSOR SN74")
    print("  Strategi: selalu buat PR dengan Time Decay yang masih fresh")
    print("=" * 74)
    print(f"{'PR #':<7} {'Tanggal':<18} {'Umur':<14} {'Decay':<10} {'Status':<10}")
    print("-" * 74)

    for r in results:
        h = r["hours"]
        d, hrs = int(h // 24), int(h % 24)
        age = f"{d}d {hrs}h" if d else f"{int(h)}h"
        print(f"#{r['number']:<5} {datetime.fromisoformat(r['createdAt'].replace('Z','+00:00')).strftime('%d/%m %H:%M'):<18} {age:<14} {r['mult']:<.4f}  {r['cat']:<10}")

    print("-" * 74)
    counts = {}
    for r in results:
        counts[r["cat"]] = counts.get(r["cat"], 0) + 1
    print(f"  FRESH (≥0.80): {counts.get('FRESH',0)} | AGING (≥0.50): {counts.get('AGING',0)} | STALE (≥0.20): {counts.get('STALE',0)} | EXPIRED (<0.20): {counts.get('EXPIRED',0)}")

    stale = [r for r in results if r["cat"] in ("STALE", "EXPIRED")]
    if stale:
        print(f"\n  ⚠️  {len(stale)} PR perlu tindakan:")
        for r in stale:
            action = "TUTUP & GANTI" if r["cat"] == "EXPIRED" else "WASPADAI"
            print(f"     #{r['number']} ({r['cat']}, mult={r['mult']:.3f}) → {action}")

    print("\n  Rekomendasi sistem:")
    print("  1. Batch PR: buat semua PR dalam 1-2 hari agar decay seragam")
    print("  2. Jika ada PR EXPIRED (>20d): tutup dan buat ulang dengan branch baru")
    print("  3. Jika ada PR STALE (>12d): siapkan pengganti lebih awal")
    print("  4. Target maksimal: semua PR dalam status FRESH atau AGING")
    print("  5. Jangan buat PR baru kalau slot masih penuh — prioritaskan yg existing")

if __name__ == "__main__":
    try:
        with open(PR_OPEN_PATH) as f:
            data = json.load(f)
        results = analyze(data)
        print_report(results)
    except FileNotFoundError:
        print("Data PR tidak ditemukan. Jalankan 'gh pr list' dulu.")
    except Exception as e:
        print(f"Error: {e}")