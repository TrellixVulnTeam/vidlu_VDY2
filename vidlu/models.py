from functools import partial, partialmethod

import torch

from vidlu import modules
from vidlu.training import initialization
from vidlu.modules import components as com
from vidlu.utils.func import (ArgTree, argtree_partialmethod, Reserved, Empty, default_args)


# Backbones ########################################################################################


def resnet_v1_backbone(depth, base_width=default_args(com.ResNetV1Backbone).base_width,
                       small_input=default_args(com.ResNetV1Backbone).small_input,
                       block_f=partial(default_args(com.ResNetV1Backbone).block_f,
                                       kernel_sizes=Reserved),
                       dim_change=None,
                       backbone_f=com.ResNetV1Backbone):
    # TODO: dropout
    basic = ([3, 3], [1, 1], 'proj')  # maybe it should be 'pad' instead of 'proj'
    bottleneck = ([1, 3, 1], [1, 1, 4], 'proj')  # last paragraph in [2]
    dim_change_arg = dim_change
    group_lengths, (ksizes, width_factors, dim_change) = {
        10: ([1] * 4, basic),  # [1] bw 64
        18: ([2] * 4, basic),  # [1] bw 64
        34: ([3, 4, 6, 3], basic),  # [1] bw 64
        110: ([18] * 3, basic),  # [1] bw 16
        50: ([3, 4, 6, 3], bottleneck),  # [1] bw 64
        101: ([3, 4, 23, 3], bottleneck),  # [1] bw 64
        152: ([3, 8, 36, 3], bottleneck),  # [1] bw 64
        164: ([18] * 3, bottleneck),  # [1] bw 16
        200: ([3, 24, 36, 3], bottleneck),  # [2] bw 64
    }[depth]
    return backbone_f(base_width=base_width, small_input=small_input, group_lengths=group_lengths,
                      width_factors=width_factors, block_f=partial(block_f, kernel_sizes=ksizes),
                      dim_change=dim_change_arg or dim_change)


resnet_v2_backbone = partial(resnet_v1_backbone,
                             block_f=partial(default_args(com.ResNetV2Backbone).block_f,
                                             kernel_sizes=Reserved),
                             backbone_f=com.ResNetV2Backbone)


def wide_resnet_backbone(depth, width_factor, small_input, dim_change='proj',
                         block_f=default_args(resnet_v2_backbone).block_f):
    zagoruyko_depth = depth

    group_count, ksizes = 3, [3, 3]
    group_depth = (group_count * len(ksizes))
    blocks_per_group = (zagoruyko_depth - 4) // group_depth
    depth = blocks_per_group * group_depth + 4
    assert zagoruyko_depth == depth, \
        f"Invalid depth = {zagoruyko_depth} != {depth} = zagoruyko_depth"

    return com.ResNetV2Backbone(base_width=16,
                                small_input=small_input,
                                group_lengths=[blocks_per_group] * group_count,
                                width_factors=[width_factor] * 2,
                                block_f=partial(block_f, kernel_sizes=ksizes),
                                dim_change=dim_change)


def densenet_backbone(depth, small_input, k=None, compression=0.5, ksizes=(1, 3),
                      block_f=partial(default_args(com.DenseNetBackbone).block_f,
                                      kernel_sizes=Reserved), backbone_f=com.DenseNetBackbone):
    # TODO: dropout 0.2
    # dropout if no pds augmentation
    depth_to_group_lengths = {
        121: ([6, 12, 24, 16], 32),
        161: ([6, 12, 36, 24], 48),
        169: ([6, 12, 32, 32], 32),
    }

    if depth in depth_to_group_lengths:
        db_lengths, default_growth_rate = depth_to_group_lengths[depth]
        k = k or default_growth_rate
    else:
        if k is None:
            raise ValueError("`k` (growth rate) must be supplied for non-Imagenet-model depth.")
        db_count = 3
        block_count = (depth - db_count - 1)
        if block_count % 3 != 0:
            raise ValueError(
                f"invalid depth: (depth-db_count-1) % 3 = {(depth - db_count - 1) % 3} != 0.")
        blocks_per_group = block_count // (db_count * len(ksizes))
        db_lengths = [blocks_per_group] * db_count
    return backbone_f(growth_rate=k,
                      small_input=small_input,
                      db_lengths=db_lengths,
                      compression=compression,
                      block_f=partial(block_f, kernel_sizes=ksizes))


mdensenet_backbone = partial(densenet_backbone,
                             block_f=partial(default_args(com.MDenseNetBackbone).block_f,
                                             kernel_sizes=Reserved),
                             backbone_f=com.MDenseNetBackbone)
fdensenet_backbone = partial(densenet_backbone,
                             block_f=partial(default_args(com.FDenseNetBackbone).block_f,
                                             kernel_sizes=Reserved),
                             backbone_f=com.FDenseNetBackbone)


# Models ###########################################################################################

class Model(modules.Module):
    def __init__(self, init=None):
        super().__init__()
        self._init = init or (lambda module: None)

    def initialize(self, input=None):
        if input is not None:
            self(input)
        self._init(module=self)


class SeqModel(modules.Sequential):
    def __init__(self, seq, init, input_adapter=None):
        inpad = {} if input_adapter is None else dict(input_adapter=input_adapter)
        super().__init__(**inpad, **seq)
        self._init = init

    initialize = Model.initialize


# Discriminative models ############################################################################

class DiscriminativeModel(SeqModel):
    def __init__(self, backbone_f, head_f, init, input_adapter=None):
        super().__init__(seq=dict(backbone=backbone_f(), head=head_f()), init=init,
                         input_adapter=input_adapter)


class ClassificationModel(DiscriminativeModel):
    pass


class SegmentationModel(DiscriminativeModel):
    def forward(self, x, shape='same'):
        h = self.backbone(x)
        return self.head(h, shape=x.shape[-2:] if 'same' else shape)


class ResNetV1(ClassificationModel):
    __init__ = partialmethod(DiscriminativeModel.__init__,
                             backbone_f=partial(resnet_v1_backbone, base_width=64),
                             init=partial(initialization.kaiming_resnet, module=Reserved))


class ResNetV2(ClassificationModel):
    __init__ = partialmethod(ResNetV1.__init__,
                             backbone_f=partial(resnet_v2_backbone, base_width=64))


class WideResNet(ResNetV2):
    __init__ = partialmethod(ResNetV2.__init__, backbone_f=wide_resnet_backbone)


class DenseNet(ClassificationModel):
    __init__ = partialmethod(DiscriminativeModel.__init__,
                             backbone_f=densenet_backbone,
                             init=partial(initialization.kaiming_densenet, module=Reserved))


class SwiftNet(SegmentationModel):
    def __init__(self,
                 backbone_f=partial(resnet_v1_backbone, base_width=64),
                 intermediate_paths=tuple(f"features.unit{i}_{j}.sum"
                                          for i, j in zip(range(3), [1] * 3)),  # TODO
                 ladder_width=128, head_f=com.heads.SegmentationHead, input_adapter=None):
        """

        intermediate_paths contains all but the last block?

        Args:
            backbone_f:
            intermediate_paths:
            ladder_width:
        """
        super().__init__(backbone_f=partial(com.KresoLadderNet,
                                            backbone_f=backbone_f,
                                            intermediate_paths=intermediate_paths,
                                            ladder_width=ladder_width,
                                            context_f=partial(
                                                com.DenseSPP, bottleneck_size=128, level_size=42,
                                                out_size=128, grid_sizes=(8, 4, 2)),
                                            up_blend_f=partial(com.LadderUpsampleBlend,
                                                               pre_blending='sum'),
                                            post_activation=True),
                         head_f=partial(head_f, kernel_size=3),
                         init=partial(initialization.kaiming_resnet, module=Reserved),
                         input_adapter=input_adapter)


class LadderDensenet(DiscriminativeModel):
    def __init__(self, backbone_f=partial(densenet_backbone), intermediate_paths=None,
                 ladder_width=128, head_f=Empty, input_adapter=None):
        """

        intermediate_paths contains all but the last block?

        Args:
            backbone_f:
            intermediate_paths:
            ladder_width:
        """
        if intermediate_paths is None:
            intermediate_paths = tuple(
                f"features.dense_block{i}.unit{j}.sum"  # TODO: automatic based on backbone
                for i, j in zip(range(3), [1] * 3))
        super().__init__(backbone_f=partial(com.KresoLadderNet,
                                            backbone_f=backbone_f,
                                            intermediate_paths=intermediate_paths,
                                            ladder_width=ladder_width),
                         head_f=head_f,
                         init=partial(initialization.kaiming_resnet, module=Reserved),
                         input_adapter=input_adapter)


# Autoencoders #####################################################################################

class Autoencoder(Model):
    def __init__(self, encoder_f, decoder_f, init):
        super().__init__(init=init)
        self.encoder, self.decoder = encoder_f(), decoder_f()

    def encode(self, x):
        return self.encoder(x)

    def decode(self, z):
        return self.decoder(z)


# Adversarial autoencoder

class AdversarialAutoencoder(Autoencoder):
    def __init__(self, encoder_f=com.AAEEncoder, decoder_f=com.AAEDecoder,
                 discriminator_f=com.AAEDiscriminator,
                 prior_rand_f=partial(torch.randn, std=0.3), init=None):
        super().__init__(encoder_f, decoder_f, init)
        self.discriminator = discriminator_f()
        self.prior_rand = prior_rand_f()

    def discriminate_z(self, z):
        return self.discriminator(z)


# GANs #############################################################################################

class GAN(Model):
    def __init__(self, generator_f, discriminator_f, z_shape, z_rand_f=torch.randn, init=Empty):
        super().__init__(init=init)
        self.z_shape, self.z_rand = z_shape, z_rand_f()
        self.generator, self.discriminator = generator_f(), discriminator_f()

    def sample_z(self, batch_size):
        self.z_rand(batch_size, *self.z_shape, device=self.device)


# Other models #####################################################################################

class SmallImageClassifier(Model):
    def __init__(self):
        super().__init__()
        from torch import nn
        self.conv1 = nn.Conv2d(3, 6, 5)
        self.pool = nn.MaxPool2d(2, 2)
        self.conv2 = nn.Conv2d(6, 16, 5)
        self.fc1 = nn.Linear(16 * 5 * 5, 120)
        self.fc2 = nn.Linear(120, 84)
        self.fc3 = nn.Linear(84, 10)

    def forward(self, x):
        import torch.nn.functional as F
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = x.view(-1, 16 * 5 * 5)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return F.log_softmax(x, dim=1)
