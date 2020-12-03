import os

import gym
import ray
import jax
import jax.numpy as jnp
import coax
import haiku as hk
import optax


# name of this script
name, _ = os.path.splitext(os.path.basename(__file__))


class ApexWorker(coax.Worker):
    def __init__(self, env, q_updater, tracer, buffer=None, param_store=None, name=None):
        self.q_updater = q_updater
        self.beta = coax.utils.StepwiseLinearFunction((0, 0.4), (1000000, 1))
        super().__init__(
            env=env,
            pi=coax.BoltzmannPolicy(self.q, temperature=0.015),
            tracer=tracer,
            buffer=buffer,
            param_store=param_store,
            name=name)

    @property
    def q(self):
        return self.q_updater.q

    @property
    def q_targ(self):
        return self.q_updater.q_targ

    def get_state(self):
        return self.q.params, self.q.function_state, self.q_targ.params, self.q_targ.function_state

    def set_state(self, state):
        self.q.params, self.q.function_state, self.q_targ.params, self.q_targ.function_state = state
        self.pi.q = self.q

    def trace(self, s, a, r, done, logp):
        self.tracer.add(s, a, r, done, logp)
        self.q_targ.soft_update(self.q, tau=0.001)
        if done:
            transition_batch = self.tracer.flush()
            td_error = self.q_updater.td_error(transition_batch)
            self.buffer_add(transition_batch, td_error)
            self.push_setattr('buffer.beta', self.beta(self.env.T))

    def learn(self, transition_batch):
        metrics, td_error = self.q_updater.update(transition_batch, return_td_error=True)
        self.learn(transition_batch.idx, td_error)
        return metrics


def make_env():
    env = gym.make('PongNoFrameskip-v4')  # AtariPreprocessing will do frame skipping
    env = gym.wrappers.AtariPreprocessing(env)
    env = coax.wrappers.FrameStacking(env, num_frames=3)
    env.spec.reward_threshold = 19.
    return env


def forward_pass(S, is_training):
    seq = hk.Sequential((
        coax.utils.diff_transform,
        hk.Conv2D(16, kernel_shape=8, stride=4), jax.nn.relu,
        hk.Conv2D(32, kernel_shape=4, stride=2), jax.nn.relu,
        hk.Flatten(),
        hk.Linear(256), jax.nn.relu,
        hk.Linear(num_actions, w_init=jnp.zeros),
    ))
    X = jnp.stack(S, axis=-1) / 255.  # stack frames
    return seq(X)


# function approximator
env = make_env()
num_actions = env.action_space.n
q = coax.Q(forward_pass, env)

# updater
qlearning = coax.td_learning.QLearning(q, q_targ=q.copy(), optimizer=optax.adam(3e-4))

# reward tracer and replay buffer
tracer = coax.reward_tracing.NStep(n=1, gamma=0.99)

# ray-remote versions of our worker and replay buffer
RemoteApexWorker = ray.remote(num_cpus=1)(ApexWorker)

import cloudpickle as pickle

pickle.dumps(qlearning)

# ray.init(num_cpus=7)

# buffer = coax.experience_replay.PrioritizedReplayBuffer(capacity=1000000, alpha=0.6)
# param_store = RemoteApexWorker.remote(make_env, qlearning, tracer, buffer, name='param_store')

# actors = [
#     RemoteApexWorker.remote(make_env, qlearning, tracer, buffer, param_store, name=f'actor_{i}')
#     for i in range(4)]

# learner = RemoteApexWorker.remote(make_env, qlearning, tracer, buffer, param_store, name='learner')

# # block until one of the remote processes terminates
# ray.wait([
#     learner.learn_loop.remote(3000000),
#     *(actor.rollout_loop.remote(3000000) for actor in actors)
# ])
