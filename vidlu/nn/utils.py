from torch import nn
from vidlu.utils.func import tryable


def param_count(module):
    from numpy import prod

    trainable, non_trainable = 0, 0
    for p in module.parameters():
        n = prod(p.size())
        if p.requires_grad:
            trainable += n
        else:
            non_trainable += n

    return trainable, non_trainable


def get_submodule(root_module: nn.Module, path: str) -> nn.Module:
    """
    Returns a submodule of `root_module` that corresponds to `path`. It works
    for other attributes (e.g. Parameters) too.
    Arguments:
        root_module (Module): a module.
        path (Tensor): a string with the name of the module module relative to
        `root_module`
    """
    for name in [tryable(int, default_value=n)(n) for n in path.split('.')]:
        if isinstance(name, str):
            root_module = getattr(root_module, name)
        elif isinstance(name, int):
            root_module = root_module[name]
    return root_module


def with_intermediate_outputs(root_module: nn.Module, submodule_paths: list):
    """
    Creates a function extending `root_module.forward` so that a pair containing
    th output of `root_module.forward` as well as well as a list of intermediate
    outputs as defined in `submodule_paths`.
    Arguments:
        root_module (Module): a module.
        submodule_paths (List[str]): a list of names (relative to `root_module`)
        of modules the outputs of which you want to get.
    """
    submodules = [get_submodule(root_module, p) for p in submodule_paths]

    def forward(*args):
        outputs = [None] * len(submodule_paths)

        def create_hook(idx):
            def hook(module, input, output):
                outputs[idx] = output

            return hook

        handles = [m.register_forward_hook(create_hook(i)) for i, m in enumerate(submodules)]
        output = root_module(*args)
        for h in handles:
            h.remove()
        return output, list(zip(submodule_paths, outputs))

    return forward
