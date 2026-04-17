from __future__ import annotations

import pytest

from ci_dashboard.common.sql_helpers import chunked


def test_chunked_splits_iterable() -> None:
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]


def test_chunked_rejects_non_positive_size() -> None:
    with pytest.raises(ValueError, match="positive"):
        list(chunked([1, 2], 0))
