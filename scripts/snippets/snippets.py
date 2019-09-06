# list aprameters with shapes
print('\n'.join(f"{k} {tuple(v.shape)}" for k, v in trainer.model.state_dict().items()))

# semseg
visualization.view_predictions(
    data.train.map(lambda r, trainer=trainer: (
    trainer.prepare_batch((r.x.reshape((1,) + r.x.shape), r.y.reshape((1,) + r.y.shape)))[
        0].squeeze().permute(1, 2, 0).detach().cpu().numpy(), r.y.cpu().numpy())),
    infer=lambda x, trainer=trainer: trainer.model(
        torch.tensor(x).to(device=trainer.model.device).permute(2, 0, 1).unsqueeze(0)).argmax(
        1).squeeze().int().cpu().numpy())

# ?
visualization.view_predictions(data.train.map(lambda r, trainer=trainer: (trainer.attack.perturb(
    *trainer.prepare_batch(
        (r.x.reshape((1,) + r.x.shape), r.y.reshape((1,) + r.y.shape)))).squeeze().permute(1, 2,
                                                                                           0).detach().cpu().numpy(),
                                                                          int(r.y.cpu().numpy()))))

visualization.view_predictions(data.train.map(lambda r, trainer=trainer: (trainer.attack.perturb(
    *trainer.prepare_batch(
        (r.x.reshape((1,) + r.x.shape), r.y.reshape((1,) + r.y.shape)))).squeeze().permute(1, 2,
                                                                                           0).detach().cpu().numpy(),
                                                                          r.y.cpu().numpy())))

# semseg, adversarial
visualization.view_predictions(
    data.train.map(lambda r, trainer=trainer: (trainer.attack.perturb(*trainer.prepare_batch(
        (r.x.reshape((1,) + r.x.shape), r.y.reshape((1,) + r.y.shape)))).squeeze().permute(1, 2,
                                                                                           0).detach().cpu().numpy(),
                                               r.y.cpu().numpy())),
    infer=lambda x, trainer=trainer: trainer.model(
        torch.tensor(x).to(device=trainer.model.device).permute(2, 0, 1).unsqueeze(0)).argmax(
        1).squeeze().int().cpu().numpy())

visualization.view_predictions(
    data.train.map(lambda r, trainer=trainer: (
    (r.x.permute(1, 2, 0).detach().cpu().numpy(), r.y.cpu().numpy()))),
    infer=lambda x, trainer=trainer: trainer.model(
        torch.tensor(x).to(device=trainer.model.device).permute(2, 0, 1).unsqueeze(0)).argmax(
        1).squeeze().int().cpu().numpy())

# print confusion matrix
np.set_printoptions(edgeitems=30, linewidth=100000)
print(repr(np.array(trainer.metrics['ClassificationMetrics'].cm, dtype=np.int64)))
# print metrics
print(trainer.metrics['ClassificationMetrics'].compute())

# hooks
trainer.model.backbone.features.unit0_0.branching.block.norm1.orig.weight
trainer.model.backbone.features.unit0_0.branching.block.norm1.orig.weight.grad
pp = {n: p for n, p in trainer.model.named_parameters() if torch.all(p == 0)}
print('\n'.join(pp.keys()))

trainer.model.backbone.features.unit0_0.branching.block.norm1.register_forward_hook(
    lambda s, inp, out, trainer=trainer: print(
        trainer.model.backbone.features.unit0_0.branching.block.norm1.orig.weight))
trainer.model.backbone.features.unit0_0.branching.block.norm1.register_forward_hook(
    lambda s, inp, out, trainer=trainer: print(
        trainer.model.backbone.features.unit0_0.branching.block.norm1.orig.bias))

trainer.model.backbone.features.unit0_0.branching.block.norm1.register_forward_hook(
    lambda s, inp, out: print(inp[0].abs().max()))
trainer.model.backbone.features.unit0_0.branching.block.norm1.register_forward_hook(
    lambda s, inp, out: print(inp[0].grad))
trainer.model.backbone.features.unit0_0.branching.block.norm1.register_forward_hook(
    lambda s, inp, out: print(inp[0].grad, out.grad))
trainer.model.backbone.features.unit0_0.branching.block.norm1.register_forward_hook(
    lambda s, inp, out: print(out))
trainer.model.backbone.features.unit0_0.branching.block.norm1.register_backward_hook(
    lambda s, ig, og: print(ig[0].grad, og))
trainer.model.backbone.features.unit0_0.branching.block.norm1.register_backward_hook(
    lambda s, ig, og: print(og))
trainer.model.backbone.features.unit0_0.branching.block.act1.register_backward_hook(
    lambda s, ig, og: print(ig[0].abs().sum()))
trainer.model.backbone.features.unit0_0.branching.block.act1.register_forward_hook(
    lambda s, inp, out: print(inp[0].abs().max()))
trainer.model.backbone.features.unit0_0.branching.block.act1.register_forward_hook(
    lambda s, inp, out: print((inp[0] > 0).float().mean()))


# print calls

def tracefunc(frame, event, arg, indent=[0]):
    if event == "call":
        indent[0] += 2
        print("-" * indent[0] + "> call function", frame.f_code.co_name)
    elif event == "return":
        print("<" + "-" * indent[0], "exit function", frame.f_code.co_name)
        indent[0] -= 2
    return tracefunc


import sys;

sys.settrace(tracefunc)


# make eval attack stronger
trainer.eval_attack.stop_on_success = False
trainer.eval_attack.step_count = 50
trainer.eval_attack.eps *= 4

# make eval attack stronger and opposite loss sign
trainer.eval_attack.stop_on_success = False
trainer.eval_attack.step_count = 50
trainer.eval_attack.eps *= 4
trainer.eval_attack.loss = lambda *a, **k: -trainer.eval_attack.loss(*a, **k)

# show adversarial examples
import torch
with torch.no_grad():
    from torchvision.utils import make_grid
    def show(img):
        import numpy as np
        import matplotlib.pyplot as plt
        npimg = img.detach().cpu().numpy()
        plt.imshow(np.transpose(npimg, (1,2,0)), interpolation='nearest')
    N = 16
    adv = state.output.x_adv[:N]
    clean = state.output.x[:N]
    diff = 0.5+(adv-clean)*255/80
    pred = state.output.other_outputs_adv.hard_prediction
    target = state.output.target

    fooled = (pred != target)[:N]
    fooled = fooled.reshape(-1, *[1]*(len(adv.shape)-1))
    fooled = fooled.float() * (adv*0+1)

    class_repr = [None]*10
    for i, c in enumerate(target):
        if class_repr[c] is None:
            class_repr[c] = state.output.x[i]
    for i, x in enumerate(class_repr):
        if x is None:
            x = 0*adv[0]

    predicted_class_representatives = list(map(class_repr.__getitem__, pred[:N]))

show(make_grid(sum((list(x) for x in [clean, adv, diff, fooled, predicted_class_representatives]), []), nrow=len(adv)))

