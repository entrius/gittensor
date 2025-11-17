# Entrius 2025

# if a language/file extension isn't covered in our mapping, default to this.
# intuition is if we haven't covered it, it's more probable to be an unimportant language
DEFAULT_PROGRAMMING_LANGUAGE_WEIGHT = 0.12

BASE_GITHUB_API_URL = 'https://api.github.com'

# Github requirements
MIN_GITHUB_ACCOUNT_AGE = 180

# Scoring constants
MAX_ISSUES_SCORED_IN_SINGLE_PR = 3
UNIQUE_PR_BOOST = 0.6

# Gittensor PR tagging
PR_TAGLINE = "Contribution by Gittensor, learn more at https://gittensor.io/"
GITTENSOR_PR_TAG_MULTIPLIER = 1.50

# Time decay constants
TIME_DECAY_MIN_MULTIPLIER = 0.1  # Oldest PRs (at lookback window edge) get 10% of their score

# Rewards & Recycle constants
PARETO_DISTRIBUTION_ALPHA_VALUE = 0.85
RECYCLE_UID = 0

LINES_CONTRIBUTED_MAX_RECYCLE = 0.9
LINES_CONTRIBUTED_RECYCLE_DECAY_RATE = 0.00001

UNIQUE_PRS_MAX_RECYCLE = 0.9
UNIQUE_PRS_RECYCLE_DECAY_RATE = 0.005

# file types for which we want to mitigate rewards b/c of exploiting/gameability
MITIGATED_EXTENSIONS = ["md", "txt", "json"]
MAX_LINES_SCORED_CHANGES = 300

# PR spam mitigation constants - basically for every open pr above threshold, linearly decrease weight multiplier to final score (before pareto and normalization)
# Only applies to open prs to supported repositories.
EXCESSIVE_PR_PENALTY_THRESHOLD = 12
EXCESSIVE_PR_PENALTY_SLOPE = 0.08333
EXCESSIVE_PR_MIN_WEIGHT = 0.01

# Anti-spam detection constants
# Typo detection
TYPO_ONLY_PR_PENALTY = 0.1  # PRs with only typos get 10% of their score
WHITESPACE_ONLY_PR_PENALTY = 0.05  # PRs with only spaces get 5% of their score
ACCEPTED_COMMENT_RATIO = 0.15 # Acceptable comments ratio threshold in a pr 
FORMATTING_ONLY_PR_PENALTY = 0.15 # PRs with formatting only changes get 15% of their score
MIN_TYPO_RATIO_THRESHOLD = 0.7  # If 70%+ of changes are typos, apply penalty
TYPO_KEYWORDS = [
    'typo', 'spelling', 'grammar', 'punctuation', 'whitespace',
    'formatting', 'indentation', 'space', 'tab', 'newline'
]

FORMATTING_KEYWORDS = [
    'format', 'prettier', 'eslint', 'black', 'autopep8',
    'lint', 'style', 'formatting', 'beautify'
]

# Translation detection
TRANSLATION_ONLY_PR_PENALTY = 0.15  # PRs with only translations get 15% of their score
MIN_TRANSLATION_RATIO_THRESHOLD = 0.8  # If 80%+ of changes are translations, apply penalty
TRANSLATION_FILE_PATTERNS = [
    '/locale/', '/locales/', '/i18n/', '/translations/', '/lang/', '/languages/',
    '.po', '.pot', '.mo', '.xliff', '.xlf', '.resx', '.properties',
    '.arb', '.json',  # Flutter/i18n
    '_en.', '_fr.', '_es.', '_de.', '_zh.', '_ja.', '_ko.', '_ru.', '_pt.',
    '_it.', '_hi.', '_tr.', '_ar.', '_bn.', '_id.', '_vi.',  # common suffixes
    'README_', 'DOC_', '/docs/', '/manuals/'  # docs often used for translation spam
]

TRANSLATION_KEYWORDS = [
    'translation', 'translate', 'localization', 'localisation', 'l10n', 'i18n',
    'locale', 'language'
]

# New regex patterns for detecting translation-like content inside patches
TRANSLATION_CONTENT_PATTERNS = [
    r'"\w+"\s*:\s*".+"',         # JSON key-value pairs
    r'<string\s+name=',          # Android XML strings
    r'^\s*\w+\s*=\s*".+"$',      # .properties style
    r'<trans-unit',              # XLIFF units
    r'msgid\s+".*"',             # gettext PO format
]

# Unicode ranges to detect non-English text (e.g., Chinese, Arabic, Cyrillic)
NON_ENGLISH_UNICODE_RANGES = [
    (0x0370, 0x03FF),  # Greek
    (0x0400, 0x04FF),  # Cyrillic
    (0x0590, 0x06FF),  # Hebrew, Arabic
    (0x0900, 0x097F),  # Devanagari (Hindi)
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3040, 0x30FF),  # Japanese Hiragana/Katakana
    (0xAC00, 0xD7AF)
]

# Repetitive spam detection (per-user per-repository)
SPAM_FILE_TYPE_PATTERNS = {
    'test': ['/test/', '/tests/', '_test.', 'test_', '.test.', '.spec.', '_spec.'],
    'doc': ['readme.md', 'readme.txt', '/docs/', '/documentation/', '.md', '.rst'],
    'translation': ['/locale/', '/locales/', '/i18n/', '/translations/', '/lang/', '.po', '.pot'],
}

# Thresholds for repetitive spam detection
REPETITIVE_SPAM_MIN_PRS = 3  # Need at least 3 PRs to detect pattern
REPETITIVE_SPAM_THRESHOLD = 0.7  # If 70%+ of PRs are same type, flag as spam
REPETITIVE_SPAM_PENALTY = 0.3  # Apply 0.3x multiplier (70% reduction)