from abc import ABC
from argparse import Namespace
import collections
import functools
from functools import reduce
import typing as T
import itertools
from os import PathLike
from fractions import Fraction
import re
import warnings
import inspect

import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
import torch.nn.modules as M
import torch.utils.checkpoint as cp

from vidlu.utils.collections import NameDict
from vidlu.utils.inspect import class_initializer_locals_c
import vidlu.utils.func as vuf
import vidlu.torch_utils as vtu

import vidlu.modules.utils as vmu

# Some of modules and functions from torch.nn are replaced with wrappers.
# Look for references of the `replaces` procedure to find the code doing it.

_replaces = []


def replaces(*names):
    for name in names:
        _replaces.append(name)
    return lambda x: x


# Module class extensions ##########################################################################


def _extract_tensors(*args, **kwargs):
    for a in itertools.chain(args, kwargs.values()):
        if isinstance(a, torch.Tensor):
            yield a
        elif isinstance(a, T.Sequence):
            for x in _extract_tensors(*a):
                yield x
        elif isinstance(a, T.Mapping):
            for x in _extract_tensors(*a.values()):
                yield x


def _try_get_device_from_args(*args, **kwargs):
    x = next(_extract_tensors(*args, **kwargs), None)
    return None if x is None else x.device


def _stochastic(*superclasses):  # TODO: implement
    class StochasticModExt(*superclasses):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._stoch_next_run_id = -1
            self._stoch_last_run_id = -1
            self._stoch_last_output = None
            # self._stochastic_scope = False

        def __call__(self, *args, stochastic_run_id=None, sample_count=None):
            if is_stochastic(self):
                self._stoch_last_run_id = self._stoch_last_run_id + 1
                return [
                    self.sample(*args, stochastic_run_id=self._stoch_last_run_id, sample_count=1)
                    for _ in sample_count]
                # if self._stochastic_scope and sample_count is None:
                #    raise ValueError("Stochastic scope modules require specifying sample_count.")
                # elif not self._stochastic_scope and sample_count is not None:
                #    raise ValueError(
                #        "sample_count needs to be None for non-stochastic-scope-modules.")
                # raise NotImplementedError("Stochastic modules need to override this method")
            if stochastic_run_id is None:
                stochastic_run_id = self._stoch_next_run_id
            if stochastic_run_id != self._stoch_last_run_id:
                self._stoch_last_output = super().__call__(*args)
            return self._stoch_last_output

        def sample(self, *args, stochastic_run_id=None, sample_count=None):
            return super().__call__(*args, stochastic_run_id=None, sample_count=None)

        def is_stochastic(self):
            return is_stochastic(self)

        def stochastic_eval(self, sample_count=None):
            for c in self.children():
                c.stochastic_eval()

        def eval(self, sample_count=None, **kwargs):
            if sample_count is not None:
                self.stochastic_eval()
            super().eval(**kwargs)

    return StochasticModExt


class SplittableMixin:
    def split(self, submodule_name):
        raise TypeError(f"Splitting not implemented for module type {type(self)}")

    def deep_split(self, submodule_path):
        raise TypeError(
            f"Deep splitting not implemented for module type {type(self)}")

    def join(self, other):
        raise TypeError(f"Joining not implemented for module type {type(self)}")


class InvertibleMixin:
    @functools.cached_property
    def inverse(self):
        try:
            return self.make_inverse()
        except AttributeError as e:
            # Turn it into a TypeError so that it doesn't get turned into a confusing
            # AttributeError saying that this module has no `inverse` attribute
            raise TypeError(f"An inverse for the module `{type(self)}` is not defined: {e}")

    def make_inverse(self):
        if hasattr(self, 'inverse_forward'):
            return Inverse(self)
        raise TypeError(f"An inverse for the module `{type(self)}` is not defined.")


# Core Modules #####################################################################################

@replaces('Module')
class Module(nn.Module, SplittableMixin, InvertibleMixin, ABC):
    # Based on https://github.com/MagNet-DL/magnet/blob/master/magnet/nodes/nodes.py
    def __init__(self):
        super().__init__()
        self._built = False
        self._check = None

    def store_args(self, attribute_name='args', args=None):
        """Can be called from the __init__ method after super().__init__ to
        store the arguments that the constructor/initializer was called with."""
        args = args if args is not None else class_initializer_locals_c()
        if attribute_name in [None, '']:
            self.__dict__.update(args)
        else:
            setattr(self, attribute_name, NameDict(args))

    def _mark_if_modified(self, out, inp_to_ver):
        if isinstance(out, torch.Tensor):
            return mark_modified(out, out._version != inp_to_ver.get(out, out._version))
        elif isinstance(out, T.Sequence):
            return type(out)(self._mark_if_modified(o, inp_to_ver) for o in out)
        elif isinstance(out, T.Mapping):
            return type(out)((k, self._mark_if_modified(o, inp_to_ver)) for k, o in out.items())

    def _call_with_check(self, *args, **kwargs):
        self._check_input(*args, **kwargs)  # single check after the parent is built
        del self._check
        inp_to_ver = {a: a._version for a in _extract_tensors(*args, **kwargs)}
        return self._mark_if_modified(super().__call__(*args, **kwargs), inp_to_ver)

    def __call__(self, *args, **kwargs):
        try:
            if self._built:
                if hasattr(self, '_check'):  # checks are performed on the second input
                    if self._check is None:
                        self._check = hash((*args, *kwargs.values()))
                    elif self._check != hash((*args, *kwargs.values())):
                        return self._call_with_check(*args, **kwargs)
                return super().__call__(*args, **kwargs)
            else:
                device = _try_get_device_from_args(*args, **kwargs)
                if type(self).build != Module.build:
                    self.build(*args, **kwargs)
                if device is not None:
                    self.to(device)
                if type(self).post_build != Module.post_build:
                    super().__call__(*args, **kwargs)
                    self.post_build(*args, **kwargs)
                    if device is not None:
                        self.to(device)
                self._built = True
                return super().__call__(*args, **kwargs)
        except Exception as e:
            print(f"Error in {vmu.try_get_module_name_from_call_stack(self)}, {type(self)}")
            raise e

    def build(self, *args, **kwargs):
        """This is run before the first evaluation"""
        pass

    def post_build(self, *args, **kwargs):
        """This is run after the first evaluation (if overridden)."""
        pass

    def _check_input(self, *args, **kwargs):
        """Checks whether the input is not in-place modified.

        This is evaluated when the module is called a second time,
        i.e. when the parent is built. It should not to modify the module.
        """
        if is_modified(args[0]):
            module_name = vmu.try_get_module_name_from_call_stack(self)
            raise RuntimeError(f"The input of {module_name} is in-place modified.")

    def add_module(self, *args, **kwargs):
        if len(args) == 2 and len(kwargs) == 0:
            name, module = args
        elif len(args) == 0 and len(kwargs) == 1:
            name, module = next(iter(kwargs.items()))
        else:
            raise RuntimeError(
                "Either 2 positional arguments or a single keyword argument is required.")
        super().add_module(name, module)

    def add_modules(self, *args, **kwargs):
        for name, module in dict(*args, **kwargs).items():
            super().add_module(name, module)

    @property
    def device(self):
        param = next(self.parameters(), None)
        return None if param is None else param[0].device

    def load_state_dict(self, state_dict_or_path, strict=True):
        """Handle a path being given instead of a file. (preferred since it
        automatically maps to the correct device). Taken from MagNet."""
        sd = state_dict_or_path
        if isinstance(sd, PathLike):
            sd = torch.load(sd, map_location=self.device)
        return super().load_state_dict(sd, strict=strict)


@replaces('Identity')
class Identity(Module, nn.Identity):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x

    def make_inverse(self):
        return self


def _to_sequential_init_args(*args, **kwargs):
    if len(kwargs) > 0:
        if len(args) > 0:
            raise ValueError(
                "If keyword arguments are supplied, no positional arguments are allowed.")
        args = [kwargs]
    if len(args) == 1 and isinstance(args[0], dict):
        args = [collections.OrderedDict(args[0])]
    return args


@replaces('ModuleList')
class ModuleTable(Module):
    def __init__(self, *args, **kwargs):
        super().__init__()
        args, kwargs = _to_sequential_init_args(*args, **kwargs), {}
        if len(args) == 1 and isinstance(args[0], T.Mapping):
            for key, module in args[0].items():
                self.add_module(key, module)
        else:
            for idx, module in enumerate(args):
                self.add_module(str(idx), module)

    def index(self, key):
        """Returns index of a child module from its name or the module itself."""
        elements = list(zip(*self._modules.items()))[int(not isinstance(key, str))]
        try:
            return elements.index(key)
        except ValueError as e:
            if isinstance(key, str):
                raise ValueError(f'The Seq contains no module named "{key}", only {elements}.\n{e}')

    def _get_item_by_idx(self, iterator, idx):
        """Get the idx-th item of the iterator"""
        size = len(self)
        if not -size <= idx < size:
            raise IndexError('index {} is out of range'.format(idx))
        idx %= size
        return next(itertools.islice(iterator, idx, None))

    def _idx_to_canonical_form(self, idx):
        try:  # convert slice with str bound to slice with int bounds
            if isinstance(idx, slice) and (isinstance(idx.start, str) or isinstance(idx.stop, str)):
                children_names = list(zip(*self.named_children()))[0]
                return slice(*(children_names.index(i) if isinstance(i, str) else i
                               for i in (idx.start, idx.stop)), idx.step)
        except ValueError:
            raise KeyError(f"Invalid index: {idx}.")
        return idx

    def __getitem__(self, idx):
        idx = self._idx_to_canonical_form(idx)
        if isinstance(idx, slice):
            return type(self)(dict(list(self._modules.items())[idx]))
        elif isinstance(idx, str):
            return self._modules[idx]
        else:
            return self._get_item_by_idx(self._modules.values(), idx)

    def __setitem__(self, idx, module):
        key = self._get_item_by_idx(self._modules.keys(), idx) if isinstance(idx, int) else idx
        return setattr(self, key, module)

    def __delitem__(self, idx):
        if isinstance(idx, slice):
            for key in self._modules.keys()[idx]:
                delattr(self, key)
        else:
            key = self._get_item_by_idx(self._modules.keys(), idx)
            delattr(self, key)

    def __len__(self):
        return len(self._modules)

    def __dir__(self):
        keys = super().__dir__()
        keys = [key for key in keys if not key.isdigit()]
        return keys

    def __iter__(self):
        return iter(self._modules.values())


@replaces('Sequential')
class Seq(ModuleTable, nn.Sequential):
    """A wrapper around torch.nn.Seq to enable passing a dict as the only
    parameter whereas in torch.nn.Seq only OrderedDict is accepted
    currently.
    It also supports slicing using strings.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._checkpoints = None

    def __getitem__(self, idx):
        idx = self._idx_to_canonical_form(idx)
        if isinstance(idx, slice):
            return Seq(dict(list(self._modules.items())[idx]))
        elif isinstance(idx, str):
            return self._modules[idx]
        else:
            return self._get_item_by_idx(self._modules.values(), idx)

    def forward(self, x):
        modules = [x for x in self._modules.values()]
        cp_iter = iter(self._checkpoints or ())
        cp_range = next(cp_iter, None)
        i = 0
        while i < len(self):
            if cp_range is not None and cp_range[0] == i:
                def run_segment(x_, cp_range_=cp_range):
                    for j in range(cp_range_[0], cp_range_[1] + 1):
                        x_ = modules[j](x_)
                    return x_

                checkpoint = vtu.StateAwareCheckpoint(modules[cp_range[0]: cp_range[1] + 1])
                x = checkpoint(run_segment, x) if torch.is_grad_enabled() else run_segment(x)
                i, cp_range = cp_range[1] + 1, next(cp_iter, None)
            else:
                x = modules[i](x)
                i += 1
        return x

    def make_inverse(self):
        result = Seq({k: m.inverse for k, m in reversed(self._modules.items())})
        return result

    def deep_split(self, submodule_path):
        next_name, path_remainder = submodule_path[0], submodule_path[1:]
        split_index = self.index(next_name)
        left, right = self[:split_index + 1], self[split_index + int(len(path_remainder) == 0):]
        if len(path_remainder) > 0:
            left[-1] = deep_split(left[-1], path_remainder)[0]
            right[0] = deep_split(right[0], path_remainder)[1]
        return left, right

    def deep_join(self, other):
        if type(other) is not Seq:
            raise ValueError("other must be of type Seq.")

        def index_to_name(module, index):
            return list(module.named_children())[index][0]

        if len(self) * len(other) == 0 or index_to_name(self, -1) != index_to_name(other, 0):
            return self.join(other)  # shallow joining suffices
        self_copy = self[:]
        self_copy[-1] = deep_join(self_copy[-1], other[0])
        return self_copy.join(other[1:])

    def split(self, submodule_name):
        ind = self.index(submodule_name)
        return self[:ind], self[ind:]

    def join(self, other):
        return Seq(dict(itertools.chain(self.named_children(), other.named_children())))

    def set_checkpoints(self, *inclusive_ranges):
        self._checkpoints = [(self.index(idx),) * 2 if isinstance(idx, str) else
                             tuple(map(self.index, idx)) if isinstance(idx[0], str) else
                             (idx,) * 2 if isinstance(idx[0], int) else
                             idx for idx in inclusive_ranges]
        max = -1
        for i, c in enumerate(self._checkpoints):
            if c[0] <= max or c[1] < c[0] or c[0] >= len(self):
                raise IndexError(f"Invalid sequence of checkpoint ranges: {self._checkpoints}."
                                 + f" Error at index {i} ({c}).")
            max = c[1]

    def clear_checkpoints(self):
        self._checkpoints = None


# Fork, parallel, reduction, ... ###################################################################


class Fork(ModuleTable):
    def forward(self, input):
        return tuple(m(input) for m in self)

    def make_inverse(self):
        return Fork({k: m.inverse() for k, m in self.named_children()})


class Parallel(ModuleTable):
    def forward(self, *inputs):
        inputs = vmu.sole_tuple_to_varargs(inputs)
        if len(self) == 1:
            return [self[0](x) for x in inputs]
        elif len(inputs) != len(self):
            raise ValueError(f"The number of inputs ({len(inputs)}) does not"
                             + " match the number of parallel modules."
                             + f"\nError in {vmu.try_get_module_name_from_call_stack(self)}.")
        return tuple(m(x) for m, x in zip(self, inputs))


class Merge(nn.Module):
    def forward(self, *inputs):
        inputs = vmu.sole_tuple_to_varargs(inputs)
        result = []
        for x in inputs:
            (result.extend if isinstance(x, tuple) else result.append)(x)
        return tuple(result)


class TupleSplit(Module):
    def __init__(self, split_indices):
        super().__init__()
        self.split_indices = split_indices

    def forward(self, x):
        result = []
        last_si = 0
        for si in self.split_indices:
            result.append(x[last_si:si])
            last_si = si
        result.append(x[self.split_indices[-1]:])
        return tuple(result)


class Reduce(Module):
    def __init__(self, func):
        self.func = func
        super().__init__()

    def forward(self, *inputs):
        inputs = vmu.sole_tuple_to_varargs(inputs)
        return reduce(self.func, inputs[1:], inputs[0].clone())


def pasum(x):
    def sum_pairs(l, r):
        return [a + b for a, b in zip(l, r)]

    def split(x):
        l, r = x[:len(x) // 2], x[len(x) // 2:]
        r, rem = r[:len(l)], r[len(l):]
        return x, r, rem

    while len(x) > 1:
        l, r, rem = split(x)
        x = sum_pairs(l, r) + rem

    return x[0]


class Sum(Module):  # TODO: rename to "Add"
    def __init__(self):
        super().__init__()

    def forward(self, *inputs):
        inputs = vmu.sole_tuple_to_varargs(inputs)
        shape = inputs[0].shape
        for oo in inputs[1:]:
            if oo.shape != shape:
                print(vmu.try_get_module_name_from_call_stack(self),
                      ' '.join(str(tuple(x.shape)) for x in inputs))
        if len(inputs) == 1:
            return inputs[0]
        y = inputs[0] + inputs[1]
        for x in inputs[2:]:
            y += x
        return y


class Concat(Module):
    def __init__(self, dim=1):
        super().__init__()
        self.dim = dim

    def forward(self, *inputs):
        inputs = vmu.sole_tuple_to_varargs(inputs)
        return torch.cat(inputs, self.dim)


class Split(Module):
    def __init__(self, split_size_or_sections: T.Union[int, T.Sequence], dim=1):
        super().__init__()
        self.split_size_or_sections, self.dim = split_size_or_sections, self.dim

    def forward(self, x):
        return x.split(self.split_size_or_sections, dim=self.dim)

    def make_inverse(self):
        return Concat(self.dim)


class Chunk(Module):
    def __init__(self, chunk_count: int, dim=1):
        super().__init__()
        self.store_args(store_in_self=True)

    def forward(self, x):
        return x.chunk(self.chunk_count, dim=self.dim)

    def make_inverse(self):
        return Concat(self.dim)


class Permute(Module):
    def __init__(self, *dims):
        super().__init__()
        self.dims = dims

    def forward(self, x):
        return x.permute(*self.dims)

    def make_inverse(self):
        dims = self.dims
        inv_dims = [-1] * len(dims)
        for i, d in enumerate(dims):
            inv_dims[d] = i
        return Permute(*inv_dims)


class Transpose(Module):
    def __init__(self, dim0, dim1):
        super().__init__()
        self.dims = (dim0, dim1)

    def forward(self, x):
        return x.transpose(*self.dims)

    def make_inverse(self):
        return self


class Reshape(Module):
    def __init__(self, shape):
        super().__init__()
        self.shape = shape

    def forward(self, x):
        return torch.reshape(x, (x.shape[0], *self.shape))


class BatchReshape(Module):
    def __init__(self, *shape_or_func: T.Union[tuple, T.Callable[[tuple], tuple]]):
        super().__init__()
        if len(shape_or_func) == 1 and callable(shape_or_func[0]):
            shape_or_func = shape_or_func[0]
        self.shape_or_func = shape_or_func

    def build(self, x):
        self.orig_shape = x.shape[1:]
        self.shape = sof(*self.orig_shape) if callable(sof := self.shape_or_func) else sof

    def forward(self, x):
        return x.reshape(x.shape[0], *self.shape)

    def inverse_forward(self, y):
        return y.reshape(y.shape[0], *self.orig_shape)


def _parse_auto_reshape_arg(dims_or_factors):
    dims_or_factors = re.findall(r" *(\([^)]*\)|[^(),]*) *(?:,|$)", dims_or_factors)
    return [-1 if x.strip() != '-1' else
            _parse_auto_reshape_arg(x[1:-1]) if x[0] == '(' else
            Fraction(x[1:].strip()) if x[0] == '*' else
            int(x) for x in dims_or_factors]


class AutoReshape(Module):
    """A reshape module that can be adaptive to input shape."""

    def __init__(self, dims_or_factors: T.Union[str, T.Sequence]):
        super().__init__()
        if isinstance(dims_or_factors, str):
            self.dims_or_factors = _parse_auto_reshape_arg(dims_or_factors)

    def build(self, x):
        def get_subshape(d, dims_or_factors):
            other = d // np.prod(f for f in dims_or_factors if f != -1)

            return sum([int(d * f) if isinstance(f, Fraction) else
                        int(d * (1 - other)) if f == -1 else
                        f for f in dims_or_factors], [])

        self.shape = [get_subshape(d, f) if isinstance(f, T.Sequence) else
                      [int(d * f) if isinstance(f, Fraction) else f] for d, f in
                      zip(x.shape, self.dims_or_factors)]

    def forward(self, x):
        self.orig_shape = x.shape
        return x.reshape(*self.shape)

    def make_inverse(self):
        return BatchReshape(*self.orig_shape)


class Contiguous(Module):
    def forward(self, x):
        return x.contiguous()

    def make_inverse(self):
        return Identity()


class InvContiguous(Identity):
    def forward(self, x):
        return x

    def make_inverse(self):
        return Contiguous()


class Index(Module):
    def __init__(self, *args):
        super().__init__()
        self.args = args

    def forward(self, x):
        return x.__getitem__(*self.args)


class To(Module):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.args, self.kwargs = args, kwargs

    def forward(self, x):
        return x.to(*self.args, **self.kwargs)


class Clamp(nn.Module):
    def __init__(self, min_=None, max_=None, inplace=False):
        super().__init__()
        self.min_, self.max_, self.inplace = min_, max_, inplace

    def forward(self, x):
        x_ = mark_modified(x, self.inplace)
        return torch.clamp(x_, min=self.min_, max=self.max_, out=x_ if self.inplace else None)


# Debugging ########################################################################################

class Print(Identity):
    def __init__(self, func, text=""):
        super().__init__()
        self.func, self.text = func, text

    def forward(self, x):
        print(self.text, self.func(x))
        return x


class PrintAround(Module):
    def __init__(self, module, before=None, after=None):
        super().__init__()
        self.module, self.before, self.after = module, before, after

    def forward(self, x):
        if self.before:
            print(self.before(x))
        y = self.module(x)
        if self.after:
            print(self.after(y))
        return y


# in-place modification marking

def mark_modified(x, mark=True):
    """Adds a `modified=True` attribute to the input tensor and returns a new
    tensor, a view on the same array without the attribute.

    Arguments:
        x (Tensor): input tensor.
        mark (bool): whether to set the modified attribute or just return the
            input. This optional argument is for convenience so that it can be
            used like `f(mark_modified(x, inplace), inplace=inplace))` instead
            of `f(mark_modified(x) if inplace else x, inplace=inplace)`.

    Example:
        >>> x = torch.randn(5,5)
        >>> x_ = mark_modified(x)
        >>> assert x_ is not x and torch.all(x_ == x)
        >>> assert is_modified(x) and not is_modified(x_)
        >>> y = x_.relu_()  # an in-place operation should be applied to x_
    """
    if mark:
        setattr(x, 'modified', True)
        return x[...]
    return x


def is_modified(x):
    return hasattr(x, 'modified')


# Wraps all modules and functions with inplace to support the "modified" annotation

def _forward_method_with_mark_modified(method):
    @functools.wraps(method)
    def forward(self, x, *args, **kwargs):
        return method(self, mark_modified(x, self.inplace), *args, **kwargs)

    return forward


def _func_with_mark_modified(func):
    @functools.wraps(func)
    def wrapper(x, *args, **kwargs):
        return func(mark_modified(x, kwargs['inplace']), *args, **kwargs)

    wrapper.__name__ = func.__name__
    return wrapper


def _wrap_torch_operations(namespace):
    for name, v in vars(F).items():
        if not name.startswith('_') and callable(v) and 'inplace' in vuf.params(v):
            namespace[name] = _func_with_mark_modified(v)
            replaces(name)
    for name, v in vars(M).items():
        if not name.startswith('_') and inspect.isclass(v) and issubclass(v, nn.Module) \
                and 'inplace' in vuf.params(v):
            namespace[name] = type(name, (v,),
                                   {'forward': _forward_method_with_mark_modified(v.forward)})
            replaces(name)


_wrap_torch_operations(vars())


# Wrapped modules ##################################################################################

def _dimensional_build(name, input, args, in_channels_name='in_channels') -> nn.Module:
    if in_channels_name in args and args[in_channels_name] is None:
        args[in_channels_name] = input.shape[1]
    dim = len(input.shape) - 2  # assuming 1 batch and 1 channels dimension
    if dim not in [1, 2, 3]:
        raise ValueError(f"Cannot infer {name} dimension from input shape.")
    name = f"{name}{dim}d"
    layer_func = nn.__getattribute__(name)
    for k in vuf.params(layer_func).keys():
        if k not in args:
            raise ValueError(f"Missing argument for {name}: {k}.")
    module = layer_func(**args)
    return module


def _get_conv_padding(padding_type, kernel_size, dilation):
    if any(k % 2 == 0 for k in ([kernel_size] if isinstance(kernel_size, int) else kernel_size)):
        raise ValueError(f"`kernel_size` must be an odd positive integer "
                         f"or a sequence of them, not {kernel_size}.")
    if padding_type not in ('half', 'full'):
        raise ValueError(f"Invalid padding_type value {padding_type}.")

    def get_padding(k, d):
        return (k - 1) * d // 2 if padding_type == 'half' else (k - 1) * d

    if any(isinstance(x, T.Sequence) for x in [kernel_size, dilation]):
        if isinstance(dilation, int):
            dilation = [dilation] * len(kernel_size)
        elif isinstance(kernel_size, int):
            kernel_size = [kernel_size] * len(dilation)
        return tuple(get_padding(k, d) for k, d in zip(kernel_size, dilation))
    else:
        return get_padding(kernel_size, dilation)


class WrappedModule(Module):
    def __init__(self, orig=None):
        super().__init__()
        self.orig = orig

    def forward(self, x):
        return self.orig(x)

    def __repr__(self):
        return "A" + repr(self.orig)


# TODO: Make separate Conv*ds
@replaces(*(f'Conv{i}d' for i in range(1, 4)))
class Conv(WrappedModule):
    def __init__(self, out_channels, kernel_size, stride=1, padding=0, dilation=1, groups=1,
                 bias=None, in_channels=None, padding_mode='zeros'):
        if bias is None:
            raise ValueError("The bias argument should be provided for the Conv module.")
        padding = (_get_conv_padding(padding, kernel_size, dilation)
                   if isinstance(padding, str) else padding)
        super().__init__()
        self.store_args()

    def build(self, x):
        self.orig = _dimensional_build("Conv", x, self.args)


@replaces(*(f'MaxPool{i}d' for i in range(1, 4)))
class MaxPool(WrappedModule):
    def __init__(self, kernel_size, stride=None, padding=0, dilation=1, return_indices=False,
                 ceil_mode=False):
        padding = (_get_conv_padding(padding, kernel_size, dilation)
                   if isinstance(padding, str) else padding)
        super().__init__()
        self.store_args()

    def build(self, x):
        self.orig = _dimensional_build("MaxPool", x, self.args)


@replaces(*(f'AvgPool{i}d' for i in range(1, 4)))
class AvgPool(WrappedModule):
    def __init__(self, kernel_size, stride=None, padding=0, ceil_mode=False,
                 count_include_pad=True, divisor_override=None):
        padding = (_get_conv_padding(padding, kernel_size, dilation=1)
                   if isinstance(padding, str) else padding)
        super().__init__()
        self.store_args()

    def build(self, x):
        self.orig = _dimensional_build("AvgPool", x, self.args)


@replaces(*(f'ConvTranspose{i}d' for i in range(1, 4)))
class ConvTranspose(WrappedModule):
    def __init__(self, out_channels, kernel_size, stride=1, padding=0, output_padding=1, groups=1,
                 bias=True, dilation=1, in_channels=None):
        super().__init__()
        self.store_args()

    def build(self, x):
        self.orig = _dimensional_build("ConvTranspose", x, self.args)


@replaces('Linear')
class Linear(WrappedModule):
    def __init__(self, out_features: int, bias=True, in_features=None):
        super().__init__()
        self.store_args()

    def build(self, x):
        self.args.in_features = self.args.in_features or np.prod(x.shape[1:])
        self.orig = nn.Linear(**{k: v for k, v in self.args.items()})

    def forward(self, x):
        if len(x.shape) != 2:
            x = x.view(x.size(0), -1)
        return super().forward(x)


@replaces(*(f'BatchNorm{i}d' for i in range(1, 4)))
class BatchNorm(WrappedModule):
    def __init__(self, eps=1e-5, momentum=0.1, affine=True, track_running_stats=True,
                 num_features=None):
        super().__init__()
        self.store_args()

    def build(self, x):
        self.orig = _dimensional_build("BatchNorm", x, self.args, 'num_features')


class GhostBatchNorm(BatchNorm):
    # Based on https://myrtle.ai/how-to-train-your-resnet-8-bag-of-tricks/
    def __init__(self, batch_size, eps=1e-5, momentum=0.1, affine=True,
                 track_running_stats=True, num_features=None):
        super().__init__(eps=eps, momentum=momentum, affine=affine,
                         track_running_stats=track_running_stats, num_features=num_features)
        self.store_args()
        self.running_mean = self.running_var = self.num_splits = None

    def build(self, x):
        self.orig = _dimensional_build("BatchNorm", x, self.args, 'num_features')
        num_splits = x.shape[0] // self.args.batch_size
        if num_splits * self.args.batch_size < x.shape[0]:
            raise RuntimeError(f"The size of tha input batch ({x.shape[0]}) must be divisible by"
                               + f" `batch_size` ({self.args.batch_size}).")
        self.register_buffer('running_mean', torch.zeros(self.orig.num_features * num_splits))
        self.register_buffer('running_var', torch.ones(self.orig.num_features * num_splits))
        self.running_mean = self.running_mean.view(num_splits, self.num_features).mean(
            dim=0).repeat(num_splits)
        self.running_var = self.running_var.view(num_splits, self.num_features).mean(dim=0).repeat(
            num_splits)
        self.num_splits = num_splits

    def forward(self, x):
        if self.training or not self.track_running_stats:
            _, C, *S = x.shape
            return F.batch_norm(
                x.view(-1, C * self.num_splits, *S), self.running_mean, self.running_var,
                self.weight.repeat(self.num_splits), self.bias.repeat(self.num_splits),
                True, self.momentum, self.eps).view(x.shape)
        else:
            return F.batch_norm(
                x, self.running_mean[:self.num_features], self.running_var[:self.num_features],
                self.weight, self.bias, False, self.momentum, self.eps)


# Additional generally useful M ##############################################################


class _Func(Module):
    def __init__(self, func, func_inv=None):
        super().__init__()
        self._func = func

    def forward(self, *args, **kwargs):
        return self._func(*args, **kwargs)


class Func(_Func):
    def __init__(self, func, func_inv=None, module=None):
        super().__init__(func)
        self._func_inv = func_inv
        self.module = (module if module else func if isinstance(func, nn.Module) else None)

    def make_inverse(self):
        if self._inv is None:
            raise RuntimeError("Inverse not defined.")
        inv = Func(self._inv, self._func, module=self.module)
        inv.inverse = self
        return inv


class Inverse(Module):
    def __init__(self, module):
        super().__init__()
        self.module = module

    def forward(self, *args, **kwargs):
        return self.module.inverse_forward(*args, **kwargs)

    def make_inverse(self):
        return self.module


# Stochastic #######################################################################################

class StochasticModule(Module, ABC):
    def __init__(self):
        super().__init__()
        self.stochastic_eval = False


def is_stochastic(module):
    return isinstance(module, StochasticModule) or any(is_stochastic(m) for m in module.children())


class _DropoutNd(StochasticModule, ABC):
    __constants__ = ['p', 'inplace']

    def __init__(self, p=0.5, inplace=False):
        super().__init__()
        if p < 0 or p > 1:
            raise ValueError("dropout probability has to be between 0 and 1, "
                             "but got {}".format(p))
        self.p = p
        self.inplace = inplace

    def extra_repr(self):
        inplace_str = ', inplace' if self.inplace else ''
        return 'p={}{}'.format(self.p, inplace_str)


@replaces('Dropout')
class Dropout(_DropoutNd):
    def forward(self, input):
        return F.dropout(mark_modified(input, self.inplace), self.p,
                         training=self.training or self.stochastic_eval, inplace=self.inplace)


@replaces('Dropout2d')
class Dropout2d(_DropoutNd):
    def forward(self, input):
        return F.dropout2d(mark_modified(input, self.inplace), self.p,
                           training=self.training or self.stochastic_eval, inplace=self.inplace)


class AdditiveGaussianNoise(StochasticModule):
    def __init__(self, std):
        super().__init__()
        self.std = std

    def forward(self, x):
        return x + torch.randn_like(x).mul_(self.std) \
            if self.training or self.stochastic_eval else x


# Utilities ########################################################################################

class Adapter(Module):
    def __init__(self, module_or_factory, input_adapter=None, output_adapter=None):
        super().__init__()
        mof = module_or_factory
        self.module = mof if isinstance(mof, nn.Module) else mof()
        self.input_adapter = input_adapter or vuf.identity
        self.output_adapter = output_adapter or vuf.identity

    def forward(self, x):
        x = self.input_adapter(x) if self.input_adapter else x
        y = self.module(x)
        return self.output_adapter(y) if self.output_adapter else y


def parameter_count(module) -> Namespace:
    trainable, non_trainable = 0, 0
    for _, p in module.named_parameters():
        n = np.prod(p.size())
        if p.requires_grad:
            trainable += n
        else:
            non_trainable += n

    return Namespace(trainable=trainable, non_trainable=non_trainable)


def get_submodule(root_module, path: T.Union[str, T.Sequence]) -> T.Union[Module, torch.Tensor]:
    """
    Returns a submodule of `root_module` that corresponds to `path`. It works
    for other attributes (e.g. Parameters) too.
    Arguments:
        root_module (Module): a module.
        path (Tensor): a string with the name of the module relative to
            `root_module`.
    """
    if isinstance(path, str):
        path = path.split('.') if path != '' else []
    for name in path:
        if not hasattr(root_module, name):
            raise AttributeError(
                f"The '{type(root_module).__name__}' instance has no submodule '{name}'. It has"
                + f"  children: {', '.join(list(k for k, v in root_module.named_children()))}.")
        root_module = getattr(root_module, name)
    return root_module


def deep_split(root: nn.Module, submodule_path: T.Union[list, str]):
    if isinstance(submodule_path, str):
        submodule_path = [] if submodule_path == '' else submodule_path.split('.')
    if len(submodule_path) == 0:
        return root, Seq()
    if not hasattr(root, 'deep_split'):
        raise NotImplementedError(f"Splitting not implemented for module type {type(root)}")
    return root.deep_split(submodule_path)


def deep_join(left: Module, right: Module):
    # if not type(left) is type(right):
    #     raise ValueError("Both modules must be of the same type.")
    if not hasattr(left, 'deep_join'):
        raise NotImplementedError(f"Joining not implemented for module type {type(left)}")
    return left.deep_join(right)


def with_intermediate_outputs(root: nn.Module,
                              submodule_paths: list,
                              inplace_modified_action: T.Literal['warn', 'error', None] = 'warn'):
    """Creates a function extending `root.forward` so that a pair
    containing the output of `root.forward` as well as well as a list of
    intermediate outputs as defined in `submodule_paths`.

    Arguments:
        root (Module): a module.
        submodule_paths (List[str]): a list of names (relative to `root`)
            of modules the outputs of which you want to get.
        inplace_modified_action: What to do if it is detected that an
            intermediate output is in-place modified by a subsequent
            operation.

    Example:
        >>> module(x)
        tensor(...)
        >>> module_wio = with_intermediate_outputs(module, ['backbone', 'head.logits'])
        >>> module_wio(x)
        tensor(...), (tensor(...), tensor(...))
    """
    if isinstance(submodule_paths, str):
        submodule_paths = [submodule_paths]

    def get_submodules():
        return [get_submodule(root, p) for p in submodule_paths]

    @functools.wraps(root)
    def wrapper(*args, **kwargs):
        submodules = vuf.tryable(get_submodules, None)()
        if submodules is None:  # in case the module is not yet built
            root(*args, **kwargs)
            submodules = get_submodules()

        outputs = [None] * len(submodule_paths)

        def create_hook(idx):
            def hook(module, input, output):
                outputs[idx] = output

            return hook

        handles = [m.register_forward_hook(create_hook(i)) for i, m in enumerate(submodules)]
        output = root(*args, **kwargs)
        for h in handles:
            h.remove()

        if inplace_modified_action:
            for o, smp in zip(outputs, submodule_paths):
                if is_modified(o):
                    message = f"The (intermediate) output of {smp} is" \
                              + f" in-place modified by a subsequent operation."
                    if inplace_modified_action.startswith('warn'):
                        warnings.warn(message)
                    else:
                        raise RuntimeError(message)

        return output, tuple(outputs)

    return wrapper


class IntermediateOutputsModuleWrapper(Module):
    def __init__(self, module, submodule_paths):
        """
        Creates a function extending `root.forward` so that a pair containing
        the output of `root.forward` as well as well as a list of intermediate
        outputs as defined in `submodule_paths`.
        Arguments:
            module (Module): a module.
            submodule_paths (List[str]): a list of module names relative to
            `root`.
        """
        super().__init__()
        self.module = module
        self.submodule_paths = submodule_paths
        self.handles, self.outputs = None, None

    def __del__(self):
        if self.handles is not None:
            for h in self.handles:
                h.remove()

    def post_build(self, *args, **kwargs):
        def create_hook(idx):
            def hook(module, input, output):
                self.outputs[idx] = output

            return hook

        submodules = [get_submodule(self.module, p) for p in self.submodule_paths]
        self.handles = [m.register_forward_hook(create_hook(i))
                        for i, m in enumerate(submodules)]

    def forward(self, *args, **kwargs):
        self.outputs = [None] * len(self.submodule_paths)
        output = self.module(*args, **kwargs)
        outputs = self.outputs
        self.outputs = None
        return output, tuple(outputs)


class CheckpointingModuleWrapper(Module):
    def __init__(self, module, checkpoint=cp.checkpoint):
        super().__init__()
        self.module = module
        self.checkpoint = checkpoint

    def forward(self, *args):
        return self.checkpoint(self.module, *args)


# Gradient modification

class _RevGrad(torch.autograd.Function):
    """From https://github.com/janfreyberg/pytorch-revgrad"""

    @staticmethod
    def forward(ctx, input_):
        return input_

    @staticmethod
    def backward(ctx, grad_output):  # pragma: no cover
        return -grad_output if ctx.needs_input_grad[0] else None


rev_grad = _RevGrad.apply


class RevGrad(Module):
    def forward(self, x):
        return rev_grad(x)


class _AmpGrad(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input_, a):
        ctx.save_for_backward(a)
        return input_

    @staticmethod
    def backward(ctx, grad_output):  # pragma: no cover
        a, = ctx.saved_variables
        return a * grad_output if ctx.needs_input_grad[0] else None


amp_grad = _AmpGrad.apply


class AmpGrad(Module):
    def forward(self, x):
        return amp_grad(x)


class StopGrad(Module):
    def forward(self, x):
        return x.detach()
