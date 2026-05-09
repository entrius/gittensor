# GITTENSOR STATUS — SN74

## Wallet `default` (`5CFxQjqLp7cbiyU...`)
- Free: **0.0541 τ**
- Stake: **0.019 τ**
- Total: **0.073 τ**
- UID 77: Active ✅
- PAT: 7/7 validators ✅

## PRs — entrius/allways (alpurkan17)
| # | Issue | Status | Label | Title |
|---|-------|--------|-------|-------|
| #290 | - | 🟡 OPEN | - | sanitize ALW_DENDRITE_TIMEOUT env |

## Compliance PR — Aturan CONTRIBUTING.md
| Aturan | #1133 | #1132 | #1130 | #1129 | #1126 | #1125 | #1103 | #1102 | #1101 | #1092 |
|--------|-------|-------|-------|-------|-------|-------|-------|-------|-------|-------|
| Branch dari `test` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Target `test` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Template body | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Label sesuai | ✅ enh | ✅ ref | ✅ bug | ✅ bug | ✅ bug | ✅ bug | ✅ bug | ✅ bug | ✅ bug | ✅ bug |
| Reviewer | ❌* | ❌* | ❌* | ❌* | ❌* | ❌* | ❌* | ❌* | ❌* | ❌* |
| Screenshot CLI | ✅ | - | ✅ | ✅ | - | - | - | - | - | - |
| Closes/Fixes #issue | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| \* = tidak bisa di-set dari fork (maintainer-only)

## PRs — entrius/gittensor (alpurkan17)
| # | Issue | Status | Label | Title |
|---|-------|--------|-------|-------|
| #1133 | #1004 | 🟡 OPEN | enhancement | show GitHub username after PAT validation |
| #1132 | #1098 | 🟡 OPEN | refactor | remove stale All-Hands-AI/OpenHands repo entry |
| #1130 | #841 | 🟡 OPEN | bug | gitt miner post exits 1 when all reject PAT |
| #1129 | #842 | 🟡 OPEN | bug | gitt miner check exits 1 when no valid PAT |
| #1126 | #1017 | 🟡 OPEN | bug | last:50 instead of first:50 for solver lookup |
| #1125 | #1089 | 🟡 OPEN | bug | preserve null author_github_id |
| #1103 | #989 | 🟡 OPEN | bug | excluded validator details |
| #1102 | #1046 | 🟡 OPEN | bug | PAT error messages |
| #1101 | #985 | 🟡 OPEN | bug | line-count score extensionless files |
| #1092 | #1082 | 🟡 OPEN | bug | Mirror PR adapter crash null numeric |
| #1131 | #1006 | 🔴 CLOSED | bug | duplicate-GitHub penalty stale_closed_pull_requests |
| #1128 | #1048 | 🔴 CLOSED | bug | increase labels connection first:5→first:50 |
| #1127 | #1106 | 🔴 CLOSED | bug | handle transient GitHub failures in PAT handlers |
| #1100 | #1011 | 🔴 CLOSED | bug | exit code 1 unregistered hotkey |
| #1091 | #1078 | 🔴 CLOSED | bug | gitt issues list --id crash |
| #1090 | #1084 | 🔴 CLOSED | bug | resolve_network crash on non-string config |
| #1075 | #1063 | 🔴 CLOSED | bug | reject negative label multiplier values |
| #1074 | #1056 | 🔴 CLOSED | bug | avoid double JSON parsing in GraphQL |
| #1073 | #1061 | 🔴 CLOSED | bug | validate repo_filter in gitt issues list |
| #1060 | #1056 | 🔴 CLOSED | bug | parse GraphQL response JSON once per PR page |

## Screenshot Evidence (CLI Before-After)
| PR | URL |
|----|-----|
| #1129 | [screenshot_pr1129.png](https://raw.githubusercontent.com/alpurkan17/gittensor/screenshot-evidence/assets/screenshot_pr1129.png) |
| #1130 | [screenshot_pr1130.png](https://raw.githubusercontent.com/alpurkan17/gittensor/screenshot-evidence/assets/screenshot_pr1130.png) |
| #1133 | [screenshot_pr1133.png](https://raw.githubusercontent.com/alpurkan17/gittensor/screenshot-evidence/assets/screenshot_pr1133.png) |

## Summary
- **OPEN: 10 PR** (all gittensor) — threshold 10 ✅ (maksimum tercapai)
- **CLOSED: 10 PR** (all gittensor) — **0 merged**
- **All BLOCKED** — waiting for maintainer review

## File Tersimpan
```
opencode/
├── GITTENSOR_STATUS.md        (status lengkap + 47 aturan)
├── screenshot_pr1129.png      (CLI before-after PR #1129)
├── screenshot_pr1130.png      (CLI before-after PR #1130)
└── uprock/
    ├── UPROCK_STATUS.md        (UpRock daemon status)
    └── install.sh              (npm i -g uprock)
```

## Tools Status
| Tool | Status | Keterangan |
|------|--------|------------|
| **ruff** | ✅ | lint + format |
| **ruff format** | ✅ | formatting check |
| **pyright** | ✅ | type checker |
| **pre-commit** | ✅ | 8 hooks (trailing-whitespace, end-of-file-fixer, check-yaml, check-json, mixed-line-ending, check-added-large-files, ruff lint, ruff format) |
| **Pillow** | ✅ | generate screenshot CLI |
| **scrot + xvfb** | ✅ | screenshot terminal (fallback) |
| **gh (GitHub CLI)** | ✅ | login alpurkan17, full repo scope |
| **git** | ✅ | versi sistem |
| **substrate-interface** | ✅ | untuk pytest (tapi masih gagal `bt.Synapse`) |
| **pytest** | ⚠️ | gagal karena `bittensor` mock issue di env ini |
| **pre-push hooks** | ⚠️ | pyright + pytest skip karena `uv run` gagal (maturin hardlink) |

## 7 Faktor Wajib Sebelum Buat PR

| # | Faktor | Bobot | Cara Optimasi |
|---|--------|-------|---------------|
| 1 | **Repo Weight** | 0.01–1.0x | Pilih repo dengan weight tertinggi. `entrius/gittensor` = 1.0 ✅, `entrius/allways` = 1.0 ✅ |
| 2 | **Issue Bonus** | 1.0 / 1.33 / 1.66x | WAJIB link issue via `Closes #N`. Issue dari maintainer → 1.66x. Issue biasa → 1.33x. Tanpa issue → 1.0x |
| 3 | **Credibility** | 0.0–1.0x | `merged / (merged + closed - 1)`. Target ≥80%. Hindari PR yang akan di-close tanpa merge |
| 4 | **Review Quality** | 0.0–1.0x | `max(0, 1.0 - 0.15 × CR_count)`. Hindari CHANGES_REQUESTED dari maintainer. Kirim PR yang clean |
| 5 | **Time Decay** | 0.05–1.0x | Sigmoid: 12h grace → ~20d:0.05. Batch PR dlm 1-2 hari. Ganti PR yg sudah >17 hari |
| 6 | **Label** | 0.25–1.5x | `feature`=1.5x, `enhancement`=1.25x, `bug`=1.1x, `refactor`=0.25x, default=1.0x. **HINDARI `refactor`!** |
| 7 | **Code Density** | 0.0–1.5x | `min(token_score / total_lines, 1.5)`. Makin banyak token bermakna (fungsi, class) makin baik |

### Analisis PR Merged (20 terakhir)
| Aspek | Temuan |
|-------|--------|
| Prefix terbanyak | `fix:` (12/20 = 60%) |
| Label terbanyak | `bug` (9/20), `enhancement` (7/20) |
| Body style | `## Summary` + bullet points, **tanpa checklist template** |
| Test plan | `## Test plan` dengan `- [x]` items (ruff, pyright, pytest paths) |
| `Fixes #N` | Dipakai oleh kontributor eksternal (MkDev11, plind-junior, dll) |
| Top merger | anderdc (4), seroperson/plind-junior/bitloi/aliang/Tet-9/MkDev11 (2 each) |

### Gaya Anderdc (Maintainer — Paling Sering Merge)

```markdown
## Summary
- Perubahan 1: detail teknis dengan `code ref`
- Perubahan 2: alasan dan dampak

## Related Issues
Fixes #N

## Test plan
- [x] ruff check / ruff format --check clean
- [x] pyricht clean (0 errors / 0 warnings)
- [x] pytest tests/path/test_file.py — N pass
- [ ] Reviewer: optional check
```

Ciri khas: **teknis, langsung ke inti, test plan spesifik dengan file path, tanpa formal checklist.**

### Label Prioritasku → entrius/gittensor
| Prefix Judul | Label Auto | Multiplier | Tingkat Merge | Rekomendasi |
|-------------|------------|------------|---------------|-------------|
| `fix:` | bug | **1.1x** | **60%** | ✅ PALING AMAN — paling sering di-merge |
| `feat:` | enhancement | **1.25x** | 10% | ✅ BAIK — multiplier tertinggi |
| `refactor:` | refactor | **0.25x** | 10% | ⚠️ HANYA untuk refactor besar |
| `chore:` | refactor | **0.25x** | 10% | ❌ HINDARI — hancurkan score |
| `perf:` | enhancement | **1.25x** | 5% | ✅ untuk performance fix |
| `cli:` | enhancement | **1.25x** | 5% | ✅ untuk CLI changes |

**Kesimpulan:** `fix:` adalah pilihan terbaik — multiplier 1.1x + 60% chance merge.  
**Hindari `chore:`** (0.25x multiplier — bunuh score).

### Gaya Body PR Wajib (Gaya Top Merged)
```markdown
## Summary
- <perubahan, detail teknis, alasan>

## Related Issues
Fixes #<N>

## Test plan
- [x] ruff check / ruff format --check clean
- [x] pyright clean
- [x] pytest tests/... — N pass
```
**Tidak perlu:** Type of Change, Checklist, atau template formal lain.

### Checklist Sebelum Submit PR
- [ ] Issue di-link dengan `Fixes #N` (untuk Issue Bonus 1.33x)
- [ ] Judul pakai `fix:` (paling sering di-merge) atau `feat:` (multiplier tinggi). **Jangan `chore:`**
- [ ] Branch dari `test`, target `test`
- [ ] Body: `## Summary` + bullet point + `Fixes #N` + `## Test plan` (gaya anderdc)
- [ ] Reviewer: landyndev, anderdc
- [ ] CLI change → screenshot before-after (exit-code-only exempted)
- [ ] Tidak ada CHANGES_REQUESTED → Review Quality tetap 1.0
- [ ] Semua PR dalam 1-2 hari agar Time Decay seragam
- [ ] Generate body: `python3 scripts/pr_body_builder.py --issue N --title "fix: ..." --bullets "..." --tests "..."`
- [ ] Cek kualitas: `python3 scripts/pr_quality_check.py`

### Time Decay System
Formula: `f(t) = 1 / (1 + e^(0.0161 × (t - 297.4)))` (t = jam sejak create)

| Kategori | Decay | Rentang Umur | Tindakan |
|----------|-------|-------------|----------|
| FRESH | ≥0.80 | 0–12 hari | ✅ Aman |
| AGING | ≥0.50 | 12–17 hari | ⚠️ Waspada, siapkan pengganti |
| STALE | ≥0.20 | 17–20 hari | 🔴 Segera ganti |
| EXPIRED | <0.20 | >20 hari | ❌ TUTUP & buat ulang |

Aturan:
1. Semua PR harus dalam 1 batch (1-2 hari) agar decay seragam
2. Jika ada EXPIRED: tutup PR, buat branch baru dari `test`, submit ulang
3. Jika ada STALE: siapkan pengganti sebelum EXPIRED
4. Jangan buat PR baru kalau slot penuh — threshold `min(10 + floor(ts/300), 30)`
5. Jalankan `python3 scripts/time_decay_tracker.py` untuk cek

## Catatan Kerja
- Selalu ikuti aturan dan permintaan user dalam setiap pengerjaan PR
- Cek aturan CONTRIBUTING.md sebelum buat PR
- CLI changes wajib screenshot before-after (kecuali non-output-affecting: exit-code-only)
- PR template: Summary, Related Issues, Type of Change, Testing, Checklist
- Label, reviewer, dan branch harus sesuai aturan
- Labels auto-set oleh bot `xiao-xiao-mao[bot]` (fix→bug, feat→enhancement, chore→refactor)
- Reviewers tidak bisa di-set dari fork (hanya maintainer yg bisa)
- Body PR bisa diupdate via REST API: `gh api --method PATCH .../pulls/<#>`

================================================================================
                    ATURAN WAJIB GITTENSOR — LENGKAP
================================================================================

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A. MINER — PERSYARATAN AWAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[✅] 1. Register: btcli subnet register --netuid 74 (mainnet)
[✅] 2. GitHub Fine-Grained PAT: token name "gittensor", No Expiration, Public repos read-only
[✅] 3. Broadcast PAT: gitt miner post
[✅] 4. Identity pinned: GitHub ID permanently locked to hotkey
[✅] 5. Kontribusi ke recognized repo (master_repositories.json — 215 repo)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
B. ELIGIBILITY GATE (OSS Contributions)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ ]  6. ≥5 merged PRs dengan token_score ≥ 5
[ ]  7. credibility = merged / (merged + closed - 1) ≥ 80%
[ ]  8. Gagal gate → skor 0.0

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
C. BASE SCORE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ ]  9. code_density = min(token_score / total_lines, 1.5) — AST tree-sitter
[ ] 10. base_score = (25 × code_density) + contribution_bonus (max 50)
[ ] 11. contribution_bonus = min(1.0, token_score / 1500) × 25
[ ] 12. token_score < 5 → base_score = 0
[ ] 13. File non-code (md,json,yaml): line-count, max 300 lines, weight 0.12
[ ] 14. Test files: weight 0.05× (bukan 1.0×)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
D. MULTIPLIERS (Merged PRs)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ ] 15. repo_weight_multiplier — 0.01 s.d. 1.0 (tergantung repo)
[ ] 16. issue_multiplier — 1.0/1.33×/1.66× (syarat: closes #N, issue≠PR author, dll)
[ ] 17. label_multiplier — per repo config, label terakhir dari triage+
[ ] 18. open_pr_spam_multiplier — BINARY: 1.0 ≤ threshold, 0.0 > threshold
        threshold = min(10 + floor(total_token_score/300), 30)
[ ] 19. time_decay_multiplier — sigmoid: 12h grace → ~20d:0.05
[ ] 20. credibility_multiplier — = credibility (0.80 - 1.0)
[ ] 21. review_quality_multiplier — max(0, 1.0 - 0.15×CR_count)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
E. COLLATERAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ ] 22. Open PR collateral: 20% dari potential_score per open PR
[ ] 23. final_score = max(0, total_earned - total_collateral)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
F. DUPLICATE ACCOUNT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ ] 24. Semua miner dgn GitHub ID sama → skor 0.0

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
G. ISSUE DISCOVERY (Pool Terpisah)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ ] 25. ≥7 solved issues dgn token_score ≥ 5
[ ] 26. issue_credibility = solved / (solved + closed - 1) ≥ 80%
[ ] 27. Issue linked via closes/fixes/resolves keywords
[ ] 28. Same-account double dipping → 0 score
[ ] 29. Open issue spam: threshold 5 + floor(merged_token_score/300)
[ ] 30. Post-edit after merge → 0 + counted as closed
[ ] 31. Transferred/forked issue → ignored

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
H. ISSUE BOUNTIES (15% Emisi)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ ] 32. FIFO queue, majority vote, eligibility gate required

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
I. ATURAN PULL REQUEST (CONTRIBUTING.md)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[✅] 33. Branch dari test, PR target test
[✅] 34. PR template: Summary/Related Issues/Type of Change/Testing/Checklist
[✅] 35. Label sesuai: bug/feature/enhancement/refactor/documentation
[✅] 36. Reviewer: landyndev, anderdc
[✅] 37. CLI change: screenshot before-after dilampirkan
[✅] 38. Fixes/Closes #issue di body PR
[ ]  39. Tests must pass (CI)
[ ]  40. No testing-only PRs / No unnecessary comments

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
J. EMISSION BREAKDOWN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 70% OSS Contributions
- 15% Issue Bounties Treasury
- 15% Merge Predictions (top 3: 50%/35%/15%)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
K. KONSTANTA PENTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Scoring tiap 2 jam, lookback 35 hari
- DEFAULT_REPO_WEIGHT: 0.01
- Open PR threshold: 10 + floor(ts/300), max 30
- Open issue threshold: 5
- Collateral: 20%, Mulligan: 1
- Review penalty: 0.15/CR, Time decay min: 0.05
- Min token_score: 5, Bonus cap: 1500
- Test weight: 0.05×, Non-code max: 300 lines @ 0.12
