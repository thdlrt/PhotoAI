import numpy as np
import pytest

from landscape_culler.scoring import assign_global_ratings
from landscape_culler.selection_policy import retention_budget


def test_decimal_percentages_match_integer_arithmetic_without_extra_photos():
    for count in range(251):
        for percent in range(5, 101):
            assert retention_budget(count, percent / 100) == (count * percent + 99) // 100


@pytest.mark.parametrize("ratio,budget", [(.07, 7), (.14, 14), (.28, 28), (.56, 56), (.333, 34)])
def test_production_keeps_exact_decimal_budget(ratio, budget):
    scores = np.linspace(.99, .01, 100)
    ratings, _, _ = assign_global_ratings(scores, scores, [list(range(100))], ratio)
    assert np.count_nonzero(ratings >= 3) == budget
    assert np.flatnonzero(ratings >= 3).tolist() == list(range(budget))


@pytest.mark.parametrize("count,ratio", [(True,.3),(-1,.3),(2.5,.3),(1,True),(1,.01),(1,1.01),(1,float('nan')),(1,float('inf'))])
def test_invalid_budget_inputs_are_rejected(count, ratio):
    with pytest.raises(ValueError): retention_budget(count, ratio)
