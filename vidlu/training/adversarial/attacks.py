from argparse import Namespace
from functools import partial

import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from torch import optim
from tqdm import trange

from vidlu.modules.loss import SoftmaxCrossEntropyLoss
from vidlu import ops
from vidlu.utils.misc import Event


# Attack implementations are based on AdverTorch (https://github.com/BorealisAI/advertorch)

def _predict_hard(predict, x):
    """Compute predicted labels given `x`. Used to prevent label leaking

    Args:
        predict: a model.
        x (Tensor): an input for the model.

    Returns:
        A tensor containing predicted labels.
    """
    with torch.no_grad():
        out = predict(x)
        _, y = torch.max(out, dim=1)
        return y


def _predict_soft(predict, x, temperature=1):
    """Computes softmax output given logits `x`.

     It computes `softmax(x/temperature)`. `temperature` is `1` by default.
     Lower temperature gives harder labels.

    Args:
        predict: a model.
        x (Tensor): an input for the model.
        temperature (float): softmax temperature.

    Returns:
        A tensor containing predicted labels.
    """
    with torch.no_grad():
        out = predict(x)
        if temperature != 1:
            out /= temperature
        return F.softmax(out, dim=1)


def classification_untargeted_is_sucess(pred, label):
    return pred.argmax(dim=1) != label


def classification_targeted_is_sucess(pred, label):
    return pred.argmax(dim=1) != label


class Attack:
    """Adversarial attack base class.

    Args:
        predict: a model.
        loss: a loss function that is increased by perturbing the input.
        clip_interval (Tuple[float, float]): a tuple representing the
            interval in which input values are valid.
        minimize_loss (bool): If `True`, by calling `perturb`, the
            input is perturbed so that the loss decreases. Otherwise (default),
            the input is perturbed so that the loss increases. It should be set
            to `True` for targeted attacks.
        get_predicted_label ((Tensor -> Tensor), Tensor -> Tensor): a function
            that¸accepts a model and an input, and returns the predicted label
            that can be used as the second argument in `loss`. Default: a
            function that returns a (hard) classifier prediction -- argmax over
            dimension 1 of the model output.

    Note:
        If `minimize_loss=True`, the base class constructor wraps `loss` so
        that the loss is multiplied by -1 so that subclasses thus don't have to
        check whether they need to maximize or minimize loss.
        When calling the base constructor, `clip_interval` should be set to
        `None` (default) if the output of `_predict` doesn't have to be clipped.
    """

    def __init__(self, model, loss=None, clip_bounds=None, is_success=None,
                 get_predicted_label=None):
        if type(self._perturb) == Attack._perturb:
            raise RuntimeError(
                "The `_perturb` (not `perturb`) method should be overridden in subclass"
                + f" `{type(self).__name__}`.")
        self.model = model
        self.loss = SoftmaxCrossEntropyLoss(reduction='sum',
                                            ignore_index=-1) if loss is None else loss
        self.clip_bounds = clip_bounds
        self.get_predicted_label = _predict_hard if get_predicted_label is None else get_predicted_label
        self.is_success = classification_untargeted_is_sucess if is_success is None else is_success
        self.perturb_completed = Event()

    def perturb(self, x, y=None):
        """Generates an adversarial example.

        Args:
            x (Tensor): input tensor.
            y (Tensor): label tensor. If `None`, the prediction obtained from
                `get_predicted_label(predict, x)` is used as the label.

        Return:
            Perturbed input.
        """
        if y is None:
            y = self.get_predicted_label(x)
        x_adv = self._perturb(x.detach().clone(), y.detach().clone())
        if self.clip_bounds is not None:
            x_adv = self._clip(x_adv)
        self.perturb_completed(Namespace(x=x, y=y, x_adv=x_adv))
        return x_adv

    def _get_output_loss_grad(self, x, y):
        x.requires_grad_()
        output = self.model(x)
        loss = self.loss(output, y)
        loss.backward()
        return output, loss, x.grad.detach()

    def _clip(self, x):
        return torch.clamp(x, *self.clip_bounds)

    def _perturb(self, x, y):
        # has to be implemented in subclasses
        raise NotImplementedError()


# GradientSignAttack ###############################################################################

class GradientSignAttack(Attack):
    """
    One step fast gradient sign method (Goodfellow et al, 2014).
    Paper: https://arxiv.org/abs/1412.6572
    """

    def __init__(self, model, loss=None, clip_bounds=None, is_success=None,
                 get_predicted_label=None, eps=0.3):
        """Create an instance of the GradientSignAttack.

        Args:
            eps (float or Tensor): attack step size.
        """
        super().__init__(model, loss, clip_bounds, is_success, get_predicted_label)
        self.eps = eps

    def _perturb(self, x, y=None):
        output, loss, grad = self._get_output_loss_grad(x, y)
        grad_sign = grad.sign()
        return x + self.eps * grad_sign


def rand_init_delta(x, p, eps, bounds, batch=False):
    """Generates a random perturbation from a unit p-ball scaled by eps.

    Args:
        x (Tensor): input.
        p (Real): p-ball p.
        eps (Real or Tensor):
        bounds (Real or Tensor): clipping bounds.
        batch: whether `x` is a batch.

    """
    kw = dict(dtype=x.dtype, device=x.device)
    with torch.no_grad():
        if batch:  # TODO: optimize
            delta = torch.stack(tuple(ops.random.uniform_sample_from_p_ball(p, x.shape[1:], **kw)
                                      for _ in range(x.shape[0])))
        else:
            delta = ops.random.uniform_sample_from_p_ball(p, x.shape, **kw)
        delta = delta.mul_(eps)

        return x + delta if bounds is None else torch.clamp(x + delta, *bounds) - x


# PGD ##############################################################################################

def perturb_iterative(x, y, predict, step_count, eps, step_size, loss, grad_preprocessing='sign',
                      delta_init=None, p=np.inf, clip_bounds=(0, 1), is_success_for_stopping=None):
    """Iteratively maximizes the loss over the input. It is a shared method for
    iterative attacks including IterativeGradientSign, LinfPGD, etc.

    Args:
        x: inputs.
        y: input labels.
        predict: forward pass function.
        step_count: number of iterations.
        eps: maximum distortion.
        step_size: attack step size per iteration.
        loss: loss function.
        grad_preprocessing (str): preprocessing of gradient before
            multiplication with step_size.'sign' for gradient sign, 'normalize'
            for p-norm normalization.
        delta_init (optional): initial delta.
        p (optional): the order of maximum distortion (inf or 2).
        clip_bounds (optional): mininum and maximum value pair.
        is_success_for_stopping (optional): a function that determines whether
            the attack is successful example-wise based on the predictions and
            the true labels. If it None, step_count of iterations is performed
            on every example.

    Returns:
        Perturbed inputs.
    """
    stop_on_success = is_success_for_stopping is not None
    loss_fn = loss
    if grad_preprocessing == 'sign':
        grad_preprocessing = lambda g: g.sign()
    elif grad_preprocessing == 'normalize':
        grad_preprocessing = lambda g: g / ops.batch.norm(g, p, keep_dims=True)

    delta = torch.zeros_like(x) if delta_init is None else delta_init
    delta.requires_grad_()
    xs, ys, deltas, successes, origins = [x], [y], [delta], [], []
    origin = torch.arange(len(x))
    import time
    times, time_strings = [], []

    def update_times():
        times[-1] = time.time() - times[-1]
        proportion = f"{100 * len(deltas[min((0, len(deltas) - 2))]) / len(deltas[0]):.0f}"
        time_strings.append(f"{proportion}: {times[-1]:.2f}")

    for step in range(step_count):
        times.append(time.time())
        delta.requires_grad_()
        pred = predict(x + delta)
        loss = loss_fn(pred, y)
        loss.backward()
        grad = delta.grad.clone()

        if stop_on_success:
            with torch.no_grad():
                success = is_success_for_stopping(pred, y)
                fail = success == False
                if not fail.all():  # keep the already succesful adversarial examples unchanged
                    successes.append(success)
                    origin = origin[fail]
                    origins.append(origin)
                    x, y, delta, grad = x[fail], y[fail], delta[fail].detach().clone(), grad[fail]
                    xs.append(x)
                    ys.append(y)
                    deltas.append(delta)
                    if success.all():
                        update_times()
                        break  # stop when all adversarial examples are successful

        with torch.no_grad():
            pgrad = grad_preprocessing(grad)
            if p == np.inf:  # try with restrict_norm instead of sign, mul
                delta += pgrad.mul_(step_size)
                delta.set_(ops.batch.project_to_p_ball(delta, eps, p=p))
                if clip_bounds is not None:
                    delta.set_((x + delta).clamp_(*clip_bounds).sub_(x))
            elif p in [1, 2]:  # try with restrict_norm_by_scaling instead of restrict_norm
                raise NotImplementedError("use pgrad, todo")
                delta += ops.batch.project_to_p_ball(delta.grad, 1, p=p).mul_(step_size)
                if clip_bounds is not None:
                    delta.set_((x + delta).clamp_(*clip_bounds).sub_(x))
                if eps is not None:
                    delta.set_(ops.batch.project_to_p_ball(delta, eps, p=p))  # !!
            else:
                raise NotImplementedError(f"Not implemented for p = {p}.")

            grad.zero_()
        if stop_on_success:
            update_times()
    else:
        step += 1

    if stop_on_success:
        successes[-1].fill_(True)
        with torch.no_grad():
            x, delta = xs.pop(0), deltas.pop(0)
            for i in range(len(successes) - 1):
                indices = origins[i][successes[i + 1]]
                delta[indices] = deltas[i][successes[i + 1]]
        # print(step, ", ".join(time_strings) + f"; total: {sum(times):.2f}")
    x_adv = x + delta
    return x_adv if clip_bounds is None else torch.clamp(x + delta, *clip_bounds)


class PGDAttack(Attack):
    def __init__(self, model, loss=None, clip_bounds=None, is_success=None,
                 get_predicted_label=_predict_hard, eps=8 / 255, step_count=40, step_size=2 / 255,
                 grad_preprocessing='sign', rand_init=True, p=np.inf, stop_on_success=False):
        """The PGD attack (Madry et al., 2017).

        The attack performs nb_iter steps of size eps_iter, while always staying
        within eps from the initial point.
        Paper: https://arxiv.org/pdf/1706.06083.pdf

        See the documentation of perturb_iterative.
        """
        super().__init__(model, loss, clip_bounds, is_success, get_predicted_label)
        self.eps = eps
        self.step_count = step_count
        self.step_size = step_size
        self.rand_init = rand_init
        self.grad_preprocessing = grad_preprocessing
        self.p = p
        self.stop_on_success = stop_on_success

    def _perturb(self, x, y=None):
        delta_init = (rand_init_delta(x, self.p, self.eps, self.clip_bounds) if self.rand_init
                      else torch.zeros_like(x))
        return perturb_iterative(x, y, self.model, step_count=self.step_count, eps=self.eps,
                                 step_size=self.step_size, loss=self.loss,
                                 grad_preprocessing=self.grad_preprocessing, p=self.p,
                                 clip_bounds=self.clip_bounds, delta_init=delta_init,
                                 is_success_for_stopping=self.is_success if self.stop_on_success else None)


# CW ###############################################################################################

CARLINI_L2DIST_UPPER = 1e10
CARLINI_COEFF_UPPER = 1e10
INVALID_LABEL = -1
REPEAT_STEP = 10
ONE_MINUS_EPS = 0.999999
UPPER_CHECK = 1e9
PREV_LOSS_INIT = 1e6
TARGET_MULT = 10000.0
NUM_CHECKS = 10


def get_carlini_loss(targeted, confidence_threshold):
    def carlini_loss(logits, y, l2distsq, c):
        y_onehot = ops.one_hot(y, logits.shape[-1])
        real = (y_onehot * logits).sum(dim=-1)

        other = ((1.0 - y_onehot) * logits - (y_onehot * TARGET_MULT)).max(1)[0]
        # - (y_onehot * TARGET_MULT) is for the true label not to be selected

        if targeted:
            loss1 = (other - real + confidence_threshold).relu_()
        else:
            loss1 = (real - other + confidence_threshold).relu_()
        loss2 = (l2distsq).sum()
        loss1 = torch.sum(c * loss1)
        loss = loss1 + loss2
        return loss

    return carlini_loss


class CarliniWagnerL2Attack(Attack):
    def __init__(self, model, loss=None, clip_bounds=None, is_success=None,
                 get_predicted_label=_predict_hard, distance_fn=ops.batch.l2_distace_sqr,
                 num_classes=None, confidence=0,
                 learning_rate=0.01, binary_search_steps=9, max_iter=10000, abort_early=True,
                 initial_const=1e-3):
        """The Carlini and Wagner L2 Attack, https://arxiv.org/abs/1608.04644

        Args:
            num_classes: number of clasess.
            confidence: confidence of the adversarial examples.
            learning_rate: the learning rate for the attack algorithm
            binary_search_steps: number of binary search times to find the optimum
            max_iter: the maximum number of iterations
            abort_early: if set to true, abort early if getting stuck in local min
            initial_const: initial value of the constant c
        """
        if loss is not None:
            raise NotImplementedError("The CW attack currently does not support a different loss"
                                      " function other than the default. Setting loss manually"
                                      " is not effective.")
        loss = loss or get_carlini_loss()
        super().__init__(model, loss, clip_bounds, is_success, get_predicted_label)

        self.distance_fn = distance_fn
        self.learning_rate = learning_rate
        self.max_iter = max_iter
        self.binary_search_steps = binary_search_steps
        self.abort_early = abort_early
        self.confidence = confidence
        self.initial_const = initial_const
        self.num_classes = num_classes
        # The last iteration (if we run many steps) repeat the search once.
        self.repeat = binary_search_steps >= REPEAT_STEP

    def _loss(self, output, y_onehot, l2distsq, loss_coef):
        return get_carlini_loss(self.targeted, self.confidence)(output, y_onehot, l2distsq,
                                                                loss_coef)

    def _is_successful(self, output, label, is_logits):
        # determine success, see if confidence-adjusted logits give the right
        # label

        if is_logits:
            output = output.detach().clone()
            if self.targeted:
                output[torch.arange(len(label)), label] -= self.confidence
            else:
                output[torch.arange(len(label)), label] += self.confidence
            pred = torch.argmax(output, dim=1)
        else:
            pred = output
            if pred == INVALID_LABEL:
                return pred.new_zeros(pred.shape).byte()

        return self.is_success(pred, label)

    def _forward_and_update_delta(self, optimizer, x_atanh, delta, y_onehot, loss_coeffs):
        optimizer.zero_grad()

        adv = ops.scaled_tanh(delta + x_atanh, *self.clip_bounds)
        l2distsq = self.distance_fn(adv, ops.scaled_tanh(x_atanh, *self.clip_bounds))
        output = self.model(adv)

        loss = self._loss(output, y_onehot, l2distsq, loss_coeffs)
        loss.backward()
        optimizer.step()

        return loss.item(), l2distsq.detach(), output.detach(), adv.detach()

    def _arctanh_clip(self, x):
        result = ops.clamp((x - self.clip_min) / (self.clip_max - self.clip_min), min=self.clip_min,
                           max=self.clip_max) * 2 - 1
        return ops.atanh(result * ONE_MINUS_EPS)

    def _update_if_smaller_dist_succeed(self, adv_img, labs, output, l2distsq, batch_size,
                                        cur_l2distsqs, cur_labels, final_l2distsqs, final_labels,
                                        final_advs):
        target_label = labs
        output_logits = output
        _, output_label = torch.max(output_logits, 1)

        mask = (l2distsq < cur_l2distsqs) & self._is_successful(output_logits, target_label, True)

        cur_l2distsqs[mask] = l2distsq[mask]  # redundant
        cur_labels[mask] = output_label[mask]

        mask = (l2distsq < final_l2distsqs) & self._is_successful(output_logits, target_label, True)
        final_l2distsqs[mask] = l2distsq[mask]
        final_labels[mask] = output_label[mask]
        final_advs[mask] = adv_img[mask]

    def _update_loss_coeffs(self, labs, cur_labels, batch_size, loss_coeffs, coeff_upper_bound,
                            coeff_lower_bound):
        # TODO: remove for loop, not significant, since only called during each
        # binary search step
        for ii in range(batch_size):
            cur_labels[ii] = int(cur_labels[ii])
            if self._is_successful(cur_labels[ii], labs[ii], False):
                coeff_upper_bound[ii] = min(coeff_upper_bound[ii], loss_coeffs[ii])

                if coeff_upper_bound[ii] < UPPER_CHECK:
                    loss_coeffs[ii] = (coeff_lower_bound[ii] + coeff_upper_bound[ii]) / 2
            else:
                coeff_lower_bound[ii] = max(coeff_lower_bound[ii], loss_coeffs[ii])
                if coeff_upper_bound[ii] < UPPER_CHECK:
                    loss_coeffs[ii] = (coeff_lower_bound[ii] + coeff_upper_bound[ii]) / 2
                else:
                    loss_coeffs[ii] *= 10

    def _perturb(self, x, y):
        batch_size = len(x)
        coeff_lower_bound = x.new_zeros(batch_size)
        coeff_upper_bound = torch.full_like(coeff_lower_bound, CARLINI_COEFF_UPPER)
        loss_coeffs = torch.full_like(y, self.initial_const, dtype=torch.float)
        final_advs = x
        x_atanh = self._arctanh_clip(x)
        y_onehot = ops.one_hot(y, self.num_classes).float()

        final_l2distsqs = torch.full((batch_size,), CARLINI_L2DIST_UPPER, device=x.device)
        final_labels = torch.full((batch_size,), INVALID_LABEL, dtype=torch.int, device=x.device)

        # Start binary search
        for outer_step in range(self.binary_search_steps):
            delta = nn.Parameter(torch.zeros_like(x))
            optimizer = optim.Adam([delta], lr=self.learning_rate)
            cur_l2distsqs = torch.full_like(final_l2distsqs, CARLINI_L2DIST_UPPER)
            cur_labels = torch.full_like(final_labels, INVALID_LABEL)
            prevloss = PREV_LOSS_INIT

            if (self.repeat and outer_step == (self.binary_search_steps - 1)):
                loss_coeffs = coeff_upper_bound
            for ii in range(self.max_iter):
                loss, l2distsq, output, adv_img = self._forward_and_update_delta(
                    optimizer, x_atanh, delta, y_onehot, loss_coeffs)
                if self.abort_early and ii % (self.max_iter // NUM_CHECKS or 1) == 0:
                    if loss > prevloss * ONE_MINUS_EPS:
                        break
                    prevloss = loss

                self._update_if_smaller_dist_succeed(adv_img, y, output, l2distsq, batch_size,
                                                     cur_l2distsqs, cur_labels, final_l2distsqs,
                                                     final_labels, final_advs)

            self._update_loss_coeffs(y, cur_labels, batch_size, loss_coeffs, coeff_upper_bound,
                                     coeff_lower_bound)

        return final_advs
