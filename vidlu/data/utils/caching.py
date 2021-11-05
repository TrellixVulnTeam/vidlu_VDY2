import shutil
from argparse import Namespace
from vidlu.utils.func import partial
from pathlib import Path
import warnings

import numpy as np
from tqdm import tqdm

from vidlu.data import DatasetFactory
from vidlu.data.record import Record
from vidlu.utils import path


# General

# not local for picklability
def _get_info(_, source_ds, name):
    return source_ds.info[name]


def _map_record(func, r):
    def map_(k):
        return lambda: func(r[k])

    return Record({f'{k}_': map_(k) for k in r.keys()})


def add_info(dest_ds, name, source_ds):
    return dest_ds.info_cache({name: partial(_get_info, source_ds=source_ds, name=name)})


def pds_add_info_lazily(parted_dataset, cache_dir, name, source_ds=None):
    pds = parted_dataset
    source_ds = source_ds.info_cache_hdd({name: _compute_pixel_stats_d}, Path(cache_dir))
    return Record({f"{k}_": lambda: add_info(pds[k], name, source_ds) for k in pds.keys()})


# Standardization ##################################################################################

def compute_pixel_stats(dataset, div255=False, progress_bar=False):
    pbar = tqdm if progress_bar else lambda x: x
    images = (np.array(r[0]) for r in pbar(dataset))
    mvn = np.array([(x.mean((0, 1)), x.var((0, 1)), np.prod(x.shape[:2])) for x in images])
    means, vars_, ns = [mvn[:, i] for i in range(3)]  # means, variances, pixel counts
    ws = ns / ns.sum()  # image weights (pixels in image / pixels in all images)
    mean = ws.dot(means)  # mean pixel
    var = vars_.mean(0) + ws.dot(means ** 2) - mean ** 2  # pixel variance
    std = np.sqrt(var)  # pixel standard deviation
    return (mean / 255, std / 255) if div255 else (mean, std)


# Pixel statistics cache ###########################################################################


# not local for picklability, used only in add_image_statistics_to_info_lazily
def _compute_pixel_stats_d(ds):
    mean, std = compute_pixel_stats(ds, div255=True, progress_bar=True)
    return Namespace(mean=mean, std=std)


def add_pixel_stats_to_info_lazily(parted_dataset, cache_dir):
    pds = parted_dataset

    try:
        stats_ds = pds.trainval if 'trainval' in pds.keys() else pds.train.join(pds.val)
    except KeyError:
        part_name, stats_ds = next(iter(pds.items()))
        warnings.warn('The parted dataset object has no "trainval" or "train" and "val" parts.'
                      + f' "{part_name}" is used instead.')

    ds_with_info = stats_ds.info_cache_hdd(dict(pixel_stats=_compute_pixel_stats_d), cache_dir)

    def cache_transform(ds):
        return ds.info_cache(
            dict(pixel_stats=partial(_get_info, source_ds=ds_with_info, name='pixel_stats')))

    return _map_record(cache_transform, pds)


# Caching ##########################################################################################

def cache_data_lazily(parted_dataset, cache_dir, min_free_space=20 * 2 ** 30):
    def transform(ds):
        elem_size = ds.example_size(sample_count=4)
        size = len(ds) * elem_size
        free_space = shutil.disk_usage(cache_dir).free

        ds_cached = ds.cache_hdd(f"{cache_dir}/datasets")
        cached_size = path.get_size(ds_cached.cache_dir)
        if cached_size > size * 0.1 or free_space + cached_size - size >= min_free_space:
            ds = ds_cached
        else:
            warnings.warn(f'The dataset {ds.identifier} will not be cached because there is not'
                          + f' much space left.'
                          + f' Available space: {(free_space + cached_size) / 2 ** 30:.3f} GiB.'
                          + f' Data size: {size / 2 ** 30:.3f} GiB.')
            ds_cached.delete_cache()
            del ds_cached
        return ds

    return _map_record(transform, parted_dataset)


class CachingDatasetFactory(DatasetFactory):
    def __init__(self, datasets_dir_or_factory, cache_dir, parted_ds_transforms=()):
        ddof = datasets_dir_or_factory
        super().__init__(ddof.datasets_dirs if isinstance(ddof, DatasetFactory) else ddof)
        self.cache_dir = cache_dir
        self.parted_ds_transforms = parted_ds_transforms

    def __call__(self, ds_name, **kwargs):
        pds = super().__call__(ds_name, **kwargs)
        for transform in self.parted_ds_transforms:
            pds = transform(pds)
        return cache_data_lazily(pds, self.cache_dir)
