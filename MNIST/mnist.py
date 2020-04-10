import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import datasets, transforms
from filelock import FileLock
import numpy as np
import time

import ray


def get_data_loader(batch_size):
    """Safely downloads data. Returns training/validation set dataloader."""
    mnist_transforms = transforms.Compose(
        [transforms.ToTensor(),
         transforms.Normalize((0.1307, ), (0.3081, ))])

    # We add FileLock here because multiple workers will want to
    # download data, and this may cause overwrites since
    # DataLoader is not threadsafe.
    with FileLock(os.path.expanduser("~/data.lock")):
        train_loader = torch.utils.data.DataLoader(
            datasets.MNIST(
                "~/data",
                train=True,
                download=True,
                transform=mnist_transforms),
            batch_size=batch_size,
            shuffle=True)
        test_loader = torch.utils.data.DataLoader(
            datasets.MNIST("~/data", train=False, transform=mnist_transforms),
            batch_size=batch_size,
            shuffle=True)
    return train_loader, test_loader


def evaluate(model, test_loader):
    """Evaluates the accuracy of the model on a validation dataset."""
    model.eval()
    correct = 0
    total = 0
    with torch.no_grad():
        for batch_idx, (data, target) in enumerate(test_loader):
            # This is only set to finish evaluation faster.
            if batch_idx * len(data) > 1024:
                break
            outputs = model(data)
            _, predicted = torch.max(outputs.data, 1)
            total += target.size(0)
            correct += (predicted == target).sum().item()
    return 100. * correct / total


class ConvNet(nn.Module):
    """Small ConvNet for MNIST."""

    def __init__(self):
        super(ConvNet, self).__init__()
        self.conv1 = nn.Conv2d(1, 3, kernel_size=3)
        self.fc = nn.Linear(192, 10)

    def forward(self, x):
        x = F.relu(F.max_pool2d(self.conv1(x), 3))
        x = x.view(-1, 192)
        x = self.fc(x)
        return F.log_softmax(x, dim=1)

    def get_weights(self):
        return {k: v.cpu() for k, v in self.state_dict().items()}

    def set_weights(self, weights):
        self.load_state_dict(weights)

    def get_gradients(self):
        grads = []
        for p in self.parameters():
            grad = None if p.grad is None else p.grad.data.cpu().numpy()
            grads.append(grad)
        return grads

    def set_gradients(self, gradients):
        for g, p in zip(gradients, self.parameters()):
            if g is not None:
                p.grad = torch.from_numpy(g)

@ray.remote
class ParameterServer(object):
    def __init__(self, lr, grad_rule, update_rule):
        self.model = ConvNet()
        if update_rule == 'sgd':
            self.optimizer = torch.optim.SGD(self.model.parameters(), lr=lr)
        elif update_rule == 'adam':
            self.optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)
        elif update_rule == 'adagrad':
            self.optimizer = torch.optim.Adagrad(self.model.parameters(), lr=lr)
        else:
            raise RuntimeError(f'Invalid ps_update_rule: {update_rule}')
        self.grad_rule = grad_rule

    def apply_gradients(self, *gradients):
        if self.grad_rule == 'sum':
            summed_gradients = [
                np.stack(gradient_zip).sum(axis=0)
                for gradient_zip in zip(*gradients)
            ]
        elif self.grad_rule == 'mean':
            summed_gradients = [
                np.stack(gradient_zip).mean(axis=0)
                for gradient_zip in zip(*gradients)
            ]
        else:
            raise RuntimeError(f'Invalid ps_grad_rule: {self.grad_rule}')

        self.optimizer.zero_grad()
        self.model.set_gradients(summed_gradients)
        self.optimizer.step()
        return self.model.get_weights()

    def get_weights(self):
        return self.model.get_weights()


@ray.remote
class DataWorker(object):
    def __init__(self, batch_size):
        self.model = ConvNet()
        self.batch_size = batch_size
        self.data_iterator = iter(get_data_loader(self.batch_size)[0])

    def compute_gradients(self, weights):
        self.model.set_weights(weights)
        try:
            data, target = next(self.data_iterator)
        except StopIteration:  # When the epoch ends, start a new epoch.
            self.data_iterator = iter(get_data_loader(self.batch_size)[0])
            data, target = next(self.data_iterator)
        self.model.zero_grad()
        output = self.model(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        return self.model.get_gradients()


# Get user inputs
batch_size = int(input('Enter batch size [default=128]:') or 128)
num_workers = int(input('Enter number of workers [default=5]:') or 5)
num_workers_ps_update = int(input('Enter number of workers to update PS. async_baseline=1, sync_baseline=5 [default=1]:') or 1)
lr = float(input('Enter learning rate [default=0.03]:') or 0.03)
stale_tolerance = int(input('Enter gradient staleness tolerance. async_baseline=9999, sync_baseline=0 [default=9999]:') or 9999)
ps_grad_rule = input('Enter PS gradient rule (sum/mean) [default=sum]:') or 'sum'
ps_update_rule = input('Enter PS update rule (sgd/adam/adagrad) [default=sgd]:') or 'sgd'

iterations = 500

ray.init(ignore_reinit_error=True)
ps = ParameterServer.remote(lr, ps_grad_rule, ps_update_rule)
workers = [DataWorker.remote(batch_size) for i in range(num_workers)]

model = ConvNet()
test_loader = get_data_loader(batch_size)[1]

print("Running Parameter Server Training.")
print("===============================================")

current_weights = ps.get_weights.remote()
num_worker_updates = {worker: 0 for worker in workers}
gradients = {}
for worker in workers:
    gradients[worker.compute_gradients.remote(current_weights)] = worker
total_iterations = (iterations * num_workers) // num_workers_ps_update

start_time_2 = time.time()

for i in range(total_iterations):
    # Find the specified number of workers with computed gradients
    min_worker_updates = min(num_worker_updates.values())
    unusable_gradients = {}
    used_gradients = {}
    while len(used_gradients) < num_workers_ps_update:
        ready_gradient_list, _ = ray.wait(list(gradients))
        for gradient_id in ready_gradient_list:
            if len(used_gradients) == num_workers_ps_update:
                break
            worker = gradients[gradient_id]
            worker_updates = num_worker_updates[worker]
            gradients.pop(gradient_id)
            if worker_updates - min_worker_updates <= stale_tolerance:
                used_gradients[gradient_id] = worker
            else:
                unusable_gradients[gradient_id] = worker
    for gradient_id, worker in unusable_gradients.items():
        gradients[gradient_id] = worker

    # Compute and apply gradients.
    current_weights = ps.apply_gradients.remote(*used_gradients.keys())
    for worker in used_gradients.values():
        gradients[worker.compute_gradients.remote(current_weights)] = worker
        num_worker_updates[worker] += 1

    if i % (total_iterations / 50) < 1:
        # Evaluate the current model. Evaluation occurs for a total of 50 times
        # during training.
        model.set_weights(ray.get(current_weights))
        accuracy = evaluate(model, test_loader)
        print("Iter {}: \taccuracy is {:.1f}".format(i, accuracy))

print("Final accuracy is {:.1f}.".format(accuracy))
print('Total time : {0} seconds'.format(time.time() - start_time_2))