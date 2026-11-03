from pathlib import Path

import numpy as np
import pytest

from landscape_culler.grouping import group_images


def features(angles):
    radians = np.deg2rad(angles)
    return np.column_stack([np.zeros((len(angles), 8)), np.cos(radians), np.sin(radians)])


def paths(count):
    return [Path(f"photos/DSC{1000+i}.ARW") for i in range(count)]


def test_gradual_scene_change_does_not_chain_distinct_endpoints():
    matrix = features([0, 25, 50, 75])
    assert np.all(np.sum(matrix[:-1, 8:] * matrix[1:, 8:], axis=1) > .82)
    assert group_images(paths(4), matrix) == [[0, 1], [2, 3]]


def test_long_burst_keeps_a_stable_scene_and_is_scale_invariant():
    angles = np.tile([-5, 0, 5, 0], 12)
    matrix = features(angles)
    matrix[:, 8:] *= np.linspace(.1, 10, len(angles))[:, None]
    assert group_images(paths(len(angles)), matrix) == [list(range(len(angles)))]


def test_long_slow_pan_is_bounded_by_group_anchor():
    matrix = features(np.arange(0, 100, 5))
    groups = group_images(paths(len(matrix)), matrix)
    assert len(groups) > 1
    for group in groups:
        assert float(matrix[group[0], 8:] @ matrix[group[-1], 8:]) >= .75
    assert [index for group in groups for index in group] == list(range(len(matrix)))


def test_parent_and_filename_gaps_remain_group_boundaries():
    names = [Path("a/DSC1000.ARW"), Path("a/DSC1001.ARW"), Path("b/DSC1002.ARW"), Path("a/DSC1010.ARW")]
    assert group_images(names, features([0, 0, 0, 0])) == [[0, 1], [3], [2]]


def test_technical_only_legacy_vectors_and_unordered_inputs():
    assert group_images(paths(3), np.zeros((3, 8))) == [[0, 1, 2]]
    order = [2, 0, 1]
    assert group_images([paths(3)[index] for index in order], features([0, 0, 0])) == [[1, 2, 0]]


@pytest.mark.parametrize("bad", [0, float("nan"), float("inf")])
def test_invalid_semantic_vector_cannot_merge_neighbors(bad):
    matrix = features([0, 0, 0])
    matrix[1, 8:] = bad
    assert group_images(paths(3), matrix) == [[0], [1], [2]]


def test_misaligned_features_fail_explicitly():
    with pytest.raises(ValueError, match="一一对应"):
        group_images(paths(3), features([0, 0]))
