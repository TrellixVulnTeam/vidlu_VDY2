import pytest

import random
from vidlu import factories
from vidlu.utils import tree


def test_get_data_single(tmpdir):
    data = factories.get_data("WhiteNoise{trainval,test}", tmpdir)
    assert list(data.keys()) == ["WhiteNoise"]
    assert tuple(data["WhiteNoise"].keys()) == ('trainval', 'test')


def test_get_datasets_single_args(tmpdir):
    example_shape = (random.randint(1, 32),) * 2 + (3,)
    data = factories.get_data(f"WhiteNoise(example_shape={example_shape}){{train,val}}", tmpdir)
    assert list(data.keys()) == [f"WhiteNoise(example_shape={example_shape})"]
    train, val = dict(tree.flatten(data)).values()
    assert train[0].x.shape == val[0].x.shape == example_shape


def test_get_datasets_multiple(tmpdir):
    with pytest.raises(ValueError):
        factories.get_data("WhiteNoise{trainval,test}, WhiteNoise{val}", tmpdir)
    data = factories.get_data("WhiteNoise{trainval,test}, WhiteNoise(example_shape=(8,8,8)){val}",
                              tmpdir)
    assert len(data) == 2 and len(tree.flatten(data)) == 3
