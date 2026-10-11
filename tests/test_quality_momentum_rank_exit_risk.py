import numpy as np
import pytest

from research.studies.quality_momentum.internal.analyze_rank_exit_risk_v4_1 import window_metrics


def test_recovery_does_not_hide_intermediate_loss_or_peak_drawdown():
    metrics = window_metrics([100, 120, 80, 110])
    assert metrics['return_'] == pytest.approx(.1)
    assert metrics['mae'] == pytest.approx(-.2)
    assert metrics['max_drawdown'] == pytest.approx(80 / 120 - 1)


def test_missing_interior_quote_invalidates_entire_risk_window():
    assert window_metrics([100, np.nan, 110]) is None


def test_rally_then_giveback_has_drawdown_without_loss_below_entry():
    metrics = window_metrics([100, 140, 110])
    assert metrics['mae'] == 0
    assert metrics['max_drawdown'] == pytest.approx(110 / 140 - 1)
