import argparse

# noinspection PyUnresolvedReferences
import _context
from vidlu.data.misc import pickle_sizeof
from vidlu.transforms import image
from vidlu.utils.presentation.visualization import view_predictions
from vidlu.utils.tree import print_tree
from vidlu.factories import get_prepared_data

import dirs

# python view_dataset.py
#   mnist all
#   inaturalist2018 train
#   voc2012 test
#   wilddash bench

parser = argparse.ArgumentParser()
parser.add_argument('ds', type=str)
parser.add_argument('part', type=str)
parser.add_argument('--jitter', type=str, default=None)
parser.add_argument('--permute', action='store_true')
args = parser.parse_args()

ds = get_prepared_data(f"{args.ds}{{{args.part}}}", datasets_dir=dirs.datasets,
                       cache_dir=dirs.cache)[0]

print("Name:", ds.name)
print("Info:")
print_tree(ds.info, depth=1)
print("Number of examples:", len(ds))
print(f"Size estimate: {pickle_sizeof(ds[0]) * len(ds) / 2 ** 30:.3f} GiB")

if 'class_count' not in ds.info:
    ds.info['class_count'] = 2

if args.jitter:
    jitter = eval("jitter." + args.jitter)
    ds = ds.map(jitter)

if args.permute:
    ds = ds.permute()


def transform(r):
    x = image.torch_to_numpy(r[0].permute(1, 2, 0))
    if len(r) == 1:
        return x, 0
    return x, r[1].numpy()


ds = ds.map(transform)
view_predictions(ds, infer=None)
