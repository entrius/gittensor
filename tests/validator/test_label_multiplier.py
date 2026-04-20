import pytest

from gittensor.constants import LABEL_MULTIPLIERS


@pytest.mark.parametrize('label,expected', list(LABEL_MULTIPLIERS.items()))
def test_known_labels(label, expected):
    assert LABEL_MULTIPLIERS.get(label, 1.0) == expected


@pytest.mark.parametrize('label', ['docs', 'question', 'wontfix', 'kind/feature'])
def test_unknown_labels_return_default(label):
    assert LABEL_MULTIPLIERS.get(label, 1.0) == 1.0
