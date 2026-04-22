from typing import TYPE_CHECKING, List, Optional

import bittensor as bt

if TYPE_CHECKING:
    from gittensor.classes import FileScoreResult, ScoreBreakdown


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
