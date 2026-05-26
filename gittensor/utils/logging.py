import contextvars
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator, List, Optional

import bittensor as bt

if TYPE_CHECKING:
    from gittensor.classes import FileScoreResult, ScoreBreakdown


# Set per-task while a miner is being scored so concurrent evaluations stay
# attributable in the log (see ``scoring_uid``). ``None`` means "not in a
# per-miner scope" — the filter then leaves the line untouched.
_scoring_uid: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar('scoring_uid', default=None)


class _UidLogFilter(logging.Filter):
    """Prefix each log line with the UID of the miner currently being scored.

    Attached to the ``bittensor`` logger, this runs synchronously in the thread
    that emitted the record — including ``asyncio.to_thread`` workers, which
    inherit the contextvar — so both on-loop and threaded lines get tagged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        uid = _scoring_uid.get()
        if uid is not None and not getattr(record, '_uid_tagged', False):
            record.msg = f'[UID {uid}] {record.msg}'
            record._uid_tagged = True
        return True


_uid_filter_installed = False


def install_uid_log_filter() -> None:
    """Idempotently attach the per-miner UID tag filter to the bittensor logger."""
    global _uid_filter_installed
    if not _uid_filter_installed:
        logging.getLogger('bittensor').addFilter(_UidLogFilter())
        _uid_filter_installed = True


@contextmanager
def scoring_uid(uid: int) -> Iterator[None]:
    """Tag every log line emitted within this scope (and its threads) with ``uid``."""
    token = _scoring_uid.set(uid)
    try:
        yield
    finally:
        _scoring_uid.reset(token)


def log_scoring_results(
    file_results: List['FileScoreResult'],
    total_score: float,
    total_raw_lines: int,
    breakdown: Optional['ScoreBreakdown'] = None,
) -> None:
    """Log scoring results for debugging."""
    bt.logging.debug(f'  ├─ Files ({len(file_results)} scored):')

    if file_results:
        max_name_len = max(len(f.filename) for f in file_results)
        for result in file_results:
            test_mark = ' [test]' if result.is_test_file else ''
            # Use "lines" for line-count files, "nodes" for token-scored files
            if result.scoring_method == 'line-count':
                count_str = f'{result.nodes_scored:>3} lines'
            else:
                count_str = f'{result.nodes_scored:>3} nodes'
            bt.logging.debug(f'  │   {result.filename:<{max_name_len}}  {count_str}  {result.score:>6.2f}{test_mark}')

    # Count files by scoring method
    line_count_files = [f for f in file_results if f.scoring_method == 'line-count']
    line_count_score = sum(f.score for f in line_count_files)

    # Build score breakdown string showing added vs deleted
    breakdown_parts = []
    if breakdown:
        # Structural breakdown
        if breakdown.structural_count > 0:
            struct_parts = []
            if breakdown.structural_added_count > 0:
                struct_parts.append(f'+{breakdown.structural_added_count}')
            if breakdown.structural_deleted_count > 0:
                struct_parts.append(f'-{breakdown.structural_deleted_count}')
            struct_str = '/'.join(struct_parts) if struct_parts else str(breakdown.structural_count)
            breakdown_parts.append(f'Struct: {struct_str} = {breakdown.structural_score:.2f}')

        # Leaf breakdown
        if breakdown.leaf_count > 0:
            leaf_parts = []
            if breakdown.leaf_added_count > 0:
                leaf_parts.append(f'+{breakdown.leaf_added_count}')
            if breakdown.leaf_deleted_count > 0:
                leaf_parts.append(f'-{breakdown.leaf_deleted_count}')
            leaf_str = '/'.join(leaf_parts) if leaf_parts else str(breakdown.leaf_count)
            breakdown_parts.append(f'Leaf: {leaf_str} = {breakdown.leaf_score:.2f}')

    # Add line-count info if there were line-count scored files
    if line_count_files:
        line_count_lines = sum(f.nodes_scored for f in line_count_files)
        breakdown_parts.append(
            f'Line-count: {len(line_count_files)} files, {line_count_lines} lines = {line_count_score:.2f}'
        )

    breakdown_str = ' | '.join(breakdown_parts) if breakdown_parts else ''

    # Calculate token score (total minus line-count score)
    token_score = total_score - line_count_score
    density = total_score / total_raw_lines if total_raw_lines > 0 else 0

    # Build score display: show token and line scores separately if both exist
    if line_count_score > 0 and token_score > 0:
        score_str = f'Token: {token_score:.2f} | Line: {line_count_score:.2f} | Total: {total_score:.2f}'
    elif line_count_score > 0:
        score_str = f'Line Score: {line_count_score:.2f}'
    else:
        score_str = f'Token Score: {token_score:.2f}'

    bt.logging.info(f'  ├─ {score_str} | Total Lines: {total_raw_lines} | Density: {density:.2f}')

    if breakdown_str:
        bt.logging.info(f'  │ └─ Breakdown: {breakdown_str}')
