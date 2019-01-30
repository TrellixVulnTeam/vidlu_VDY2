from .datasets import *
from .. import PartedDataset
from argparse import Namespace


def _info(cls, path=None, kwargs=None):
    return Namespace(cls=cls, path=path, kwargs=kwargs or dict())


_ds_to_info = {
    'cifar10': _info(Cifar10Dataset, '/cifar-10-batches-py'),
    'cifar100': _info(Cifar100Dataset, '/cifar-100-python'),
    'tinyimagenet': _info(TinyImageNetDataset, '/tiny-imagenet-200'),
    'tinyimages': _info(TinyImagesDataset, '/tiny-images'),
    'inaturalist2018': _info(INaturalist2018Dataset, '/inaturalist18'),
    'mozgalorvc': _info(MozgaloRVCDataset, '/mozgalo_robust_ml_challenge',
                        {"remove_bottom_proportion": 0.5, "downsampling_factor": 4}),
    'cityscapesseg': _info(CityscapesFineDataset, '/cityscapes',
                           {"downsampling_factor": 2, "remove_hood": True}),
    'wilddash': _info(WildDashDataset, '/wilddash', kwargs=dict(downsampling_factor=2)),
    'camvid': _info(CamVidDataset, '/CamVid'),
    'voc2012': _info(VOC2012SegmentationDataset, '/VOC2012'),
    'iccv09': _info(ICCV09Dataset, '/iccv09'),
    'isun': _info(ISUNDataset, '/iSUN'),
    'lsun': _info(LSUNDataset, '/LSUN'),
    'whitenoise': _info(WhiteNoiseDataset),
    'rademachernoise': _info(RademacherNoiseDataset),
    'hblobs': _info(HBlobsDataset),
}

_default_parts = ['all', 'trainval', 'train', 'val', 'test']
_default_splits = {
    'all': (('trainval', 'test'), 0.8),
    'trainval': (('train', 'val'), 0.8),
}


class DatasetFactory:

    def __init__(self, datasets_dir):
        self.datasets_dir = datasets_dir

    def __call__(self, name: str, **kwargs):
        name = name.lower()
        try:
            info = _ds_to_info[name]
        except KeyError:
            raise KeyError(f'No dataset has the name "{name}".')
        subsets = info.cls.subsets
        path_args = [self.datasets_dir + info.path] if info.path else []
        if len(info.cls.subsets) == 0:
            subsets = ['all']
            load = lambda s: info.cls(*path_args, **{**info.kwargs, **kwargs})
        else:
            load = lambda s: info.cls(*path_args, s, **{**info.kwargs, **kwargs})
        splits = getattr(info.cls, 'splits', _default_splits)
        return PartedDataset({s: load(s) for s in subsets}, splits)
