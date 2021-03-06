import numpy as np
from tqdm import trange

from helpers.convert_to_var_foo import convert_to_var
from .create_model_data import get_one_hot_object
from .loss import loss_function
from .plots import plot_learning_history, plot_learning_history_with_pgs
import copy

import torch
from torch import optim

def iterate_minibatches(inputs, batchsize, shuffle=True):
    if shuffle:
        indices = np.random.permutation(len(inputs))
    for start_idx in trange(0, len(inputs) - batchsize + 1, batchsize):
        if shuffle:
            excerpt = indices[start_idx:start_idx + batchsize]
        else:
            excerpt = slice(start_idx, start_idx + batchsize)
        yield inputs[excerpt]


def get_obj_log_prob(model, obj, kl_weight=1, return_loss=False):
    dim = obj.shape[0]
    x = np.array([obj])
    v = convert_to_var(x)

    probs, log_probs, mu, logvar = model(v)

    if return_loss:
        loss, ll, kld = loss_function(log_probs, v, mu, logvar,
                                      kl_weight=kl_weight, n_samples=model.n_samples)
        
    if torch.cuda.is_available():
        log_probs = log_probs.data.cpu().numpy()[0].mean(axis=0)
    else:
        log_probs = log_probs.data.numpy()[0].mean(axis=0)
    log_probs = log_probs[np.arange(dim), obj.astype('int64')]
    obj_log_prob = log_probs.sum()

    if return_loss:
        return obj_log_prob, loss, ll, kld
    else:
        return obj_log_prob


def train_epoch(X_train, model, optimizer, batchsize=32):
    ll_log = []
    kld_log = []
    pgs_log = []

    for x_batch in iterate_minibatches(X_train, batchsize=batchsize, shuffle=True):
        model.train()
        data = convert_to_var(x_batch.astype(np.float32))
        optimizer.zero_grad()
        recon_batch, log_probs, mu, logvar = model(data)

        before_log_probs = []
        for x in x_batch:
            before_log_probs.append(get_obj_log_prob(model, x))
        before_log_probs = np.array(before_log_probs)

        loss, ll, kld = loss_function(log_probs, data, mu, logvar, n_samples=model.n_samples)
        loss.backward()
        optimizer.step()

        after_log_probs = []
        for x in x_batch:
            after_log_probs.append(get_obj_log_prob(model, x))
        after_log_probs = np.array(after_log_probs)

        ll_log.append(ll.data[0])
        kld_log.append(kld.data[0])
        pgs_log.append(after_log_probs - before_log_probs)
    return ll_log, kld_log, pgs_log


def train(X_train, X_test,
          model, optimizer, num_epochs=5, batchsize=32,
          img_name=None):
    ll_log = []
    kld_log = []
    pgs_log = []

    for i in range(num_epochs):
        epoch_results = train_epoch(X_train, model, optimizer, batchsize=batchsize)
        ll_log += epoch_results[0]
        kld_log += epoch_results[1]
        pgs_log += epoch_results[2]

    model.eval()
    recon_batch, log_probs, mu, logvar = model(convert_to_var(X_test))
    recon_batch = recon_batch.data.numpy()
    log_probs = log_probs.data.numpy()
    mu = mu.data.numpy()
    logvar = logvar.data.numpy()
    std = np.exp(0.5 * logvar)
    plot_learning_history((ll_log, kld_log),
                          mu=mu, std=std, filename=img_name)
    return ll_log, kld_log, pgs_log


def train_online(schedule, X_test,
                 model, optimizer,
                 kl_weight=1, alpha=0.1, img_name=None, red_lines=[]):
    np.random.seed(11)

    X_test = np.array(X_test)
    num_classes, dim = X_test.shape[0], X_test.shape[1]

    pgs = [[] for _ in range(num_classes)]
    online_pgs = []
    total_loss_gains = []
    kl_gains = []

    train_logs = [[] for _ in range(2)]
    all_log_probs = [[] for _ in range(len(X_test))]

    for t in range(len(schedule)):
        if t % 100 == 0:
            print('t={}'.format(t))
        model.train()
        cur_class = schedule[t]
        obj = get_one_hot_object(cur_class, dim, num_classes)

        log_prob_before, loss, ll, kld = get_obj_log_prob(model, obj,
                                                          kl_weight=kl_weight, return_loss=True)

        if torch.cuda.is_available():
            train_logs[0].append(ll.cpu().data.numpy()[0])
            train_logs[1].append(kld.cpu().data.numpy()[0])
        else:
            train_logs[0].append(ll.data.numpy()[0])
            train_logs[1].append(kld.data.numpy()[0])

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        log_prob_after, loss_next, ll_next, kld_next = get_obj_log_prob(model, obj,
                                                                        kl_weight=kl_weight, return_loss=True)

        pg = log_prob_after - log_prob_before
        pgs[cur_class].append(pg)
        online_pgs.append(pg)
        
        
        if torch.cuda.is_available():
            loss_gain = loss_next.cpu().data.numpy()[0] - loss.cpu().data.numpy()[0]
            total_loss_gains.append(loss_gain)

            kld_gain = kld_next.cpu().data.numpy()[0] - kld.cpu().data.numpy()[0]
            kl_gains.append(kld_gain)
        else:
            loss_gain = loss_next.data.numpy()[0] - loss.data.numpy()[0]
            total_loss_gains.append(loss_gain)

            kld_gain = kld_next.data.numpy()[0] - kld.data.numpy()[0]
            kl_gains.append(kld_gain)

        for i, obj in enumerate(X_test):
            if i == cur_class:
                continue
            copy_model = copy.deepcopy(model)
            copy_optimizer = optim.RMSprop(copy_model.parameters())
            copy_optimizer.load_state_dict(optimizer.state_dict())
            log_prob_before, loss, ll, kld = get_obj_log_prob(copy_model, obj,
                                                              kl_weight=kl_weight, return_loss=True)
            copy_optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            log_prob_after, loss_next, ll_next, kld_next = get_obj_log_prob(copy_model, obj,
                                                                            kl_weight=kl_weight, return_loss=True)
            pg = log_prob_after - log_prob_before
            pgs[i].append(pg)

    plot_learning_history_with_pgs(train_logs, online_pgs,
                                   alpha=alpha, red_lines=red_lines, filename=img_name)

    online_pgs = np.array(online_pgs)
    pct = (online_pgs < 0).mean()

    lls = np.array(train_logs[0])
    mean_last_ll = lls[-10:].mean()

    return mean_last_ll, pct, all_log_probs, pgs, total_loss_gains, kl_gains


def train_online_alternately(schedule, X_test,
                             model, kl_optimizer, ll_optimizer,
                             kl_weight=1, alpha=0.1, img_name=None):
    np.random.seed(11)

    X_test = np.array(X_test)
    num_classes, dim = X_test.shape[0], X_test.shape[1]

    pgs = []
    total_loss_gains = []
    kl_gains = []

    train_logs = [[] for _ in range(2)]
    all_log_probs = [[] for _ in range(len(X_test))]

    for t in range(len(schedule)):
        model.train()
        cur_class = schedule[t]
        obj = get_one_hot_object(cur_class, dim, num_classes)

        _, loss, ll, kld = get_obj_log_prob(model, obj,
                                            kl_weight=kl_weight, return_loss=True)

        train_logs[0].append(ll.data.numpy()[0])
        train_logs[1].append(kld.data.numpy()[0])

        kl_optimizer.zero_grad()
        kl_loss = kl_weight * kld
        kl_loss.backward()
        kl_optimizer.step()

        log_prob_before, loss, ll, kld = get_obj_log_prob(model, obj,
                                            kl_weight=kl_weight, return_loss=True)

        ll_optimizer.zero_grad()
        neg_ll = -1 * ll
        neg_ll.backward()
        ll_optimizer.step()

        log_prob_after, loss_next, ll_next, kld_next = get_obj_log_prob(model, obj,
                                                                        kl_weight=kl_weight, return_loss=True)

        pg = ll_next.data.numpy()[0] - ll.data.numpy()[0]#log_prob_after - log_prob_before
        pgs.append(pg)

        loss_gain = loss_next.data.numpy()[0] - loss.data.numpy()[0]
        total_loss_gains.append(loss_gain)

        kld_gain = kld_next.data.numpy()[0] - kld.data.numpy()[0]
        kl_gains.append(kld_gain)

        for i, obj in enumerate(X_test):
            log_prob = get_obj_log_prob(model, obj, return_loss=False)
            all_log_probs[i].append(log_prob)

    plot_learning_history_with_pgs(train_logs, pgs,
                                   alpha=alpha, filename=img_name)

    pgs = np.array(pgs)
    pct = (pgs < 0).mean()

    lls = np.array(train_logs[0])
    mean_last_ll = lls[-10:].mean()

    return mean_last_ll, pct, all_log_probs, pgs, total_loss_gains, kl_gains
