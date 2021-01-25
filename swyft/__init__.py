from .cache import DirectoryCache, MemoryCache
from .estimation import RatioEstimator, Points
from .intensity import Prior
from .network import OnlineNormalizationLayer, DefaultHead, DefaultTail
from .plot import corner
from .utils import Module, format_param_list

__all__ = [
    "Prior",
    "Module",
    "DirectoryCache",
    "DefaultHead",
    "DefaultTail",
    "OnlineNormalizationLayer",
    "MemoryCache",
    "RatioEstimator",
    "Points",
    "corner",
    "Posterior",
    "amortize",
]

class Posterior:
    """Posterior container"""
    def __init__(self, re, prior):
        """Posterior container initialization.

        Args:
            re (RatioEstimator)
            prior (Prior)
        """
        self._re = re
        self._prior = prior

    @property
    def prior(self):
        return self._prior

    @property
    def re(self):
        return self._re

    def __call__(self, obs, n_samples = 100000):
        return self._re.posterior(obs, self._prior, n_samples = n_samples)

    def state_dict(self):
        return dict(re = re.state_dict(), prior = prior.state_dict())

    @classmethod
    def from_state_dict(cls, state_dict):
        return Posterior(
                RatioEstimator.from_state_dict(state_dict['re']),
                Prior.from_state_dict(state_dict['prior'])
                )

    def gen_constr_prior(self, obs, th = -10):
        return self._prior.get_masked(obs, self._re, th = th)


def _amortize(model, prior, cache, param_list = None, noise = None, N = 3000,
        train_args = {}, head = DefaultHead, tail = DefaultTail, head_args =
        {}, tail_args = {}, device = 'cpu'):

    cache.grow(prior, N)
    cache.simulate(model)
    indices = cache.sample(prior, N)
    points = Points(indices, cache, noise)

    if param_list is None:
        param_list = prior.params()

    re = RatioEstimator(param_list, device=device, head = head, tail = tail, tail_args = tail_args, head_args = head_args)
    re.train(points, **train_args)

    return Posterior(re, prior)


class NestedRatios:
    """Main SWYFT interface class."""
    def __init__(self, model, prior, obs, noise = None, cache = None, device = 'cpu'):
        """Initialize swyft.

        Args:
            model (function): Simulator function.
            noise (function): Noise model.
            prior (Prior): Prior model.
            cache (Cache): Storage for simulator results.
            obs (dict): Target observation (can be None for amortized inference).
            device (str): Device.
        """
        # Not stored
        self._model = model
        self._noise = noise
        self._obs = obs
        if cache is None:
            cache = MemoryCache.from_simulator(model, prior, noise = noise)
        self._cache = cache
        self._device = device

        # Stored as part of object
        self._prior = prior  # Initial prior
        self._posterior = None  # Available after training

    @property
    def obs(self):
        return self._obs

    @property
    def marginals(self):
        if self._posterior is None:
            print("NOTE: To generated marginals from NRE, call .run(...).")
        return self._posterior

    @property
    def prior(self):
        return self._prior

    def run(self, Ninit = 3000, train_args={}, head=DefaultHead,
            tail=DefaultTail, head_args={}, tail_args={}, f = 1.5, vr = 0.9,
            max_rounds = 10, Nmax = 30000, keep_history = False, vmax = 0.5):
        """Perform 1-dim marginal focus.

        Args:
            Ninit (int): Number of initial training points.
            head (swyft.Module instance or type): Head network (optional).
            tail (swyft.Module instance or type): Tail network (optional).
            head_args (dict): Keyword arguments for head network instantiation.
            tail_args (dict): Keyword arguments for tail network instantiation.
            f (float > 1): Maximum increase of training data each round.
            vr (float < 1): Threshold constrained posterior volume reduction as stopping criterion.
            max_rounds (int): Maximum number of rounds
            Nmax (int): Maximum size of training data per round.
        """

        # TODO: Add optional param_list, and non 1d focus rounds
        param_list = self._cache.params

        if self._posterior is not None:
            print("Nothing to do.")
            return

        assert vr < 1.
        assert f >= 1.

        prior = self._prior

        N = Ninit
        for r in range(max_rounds):
            print("N =", N)
            posterior = _amortize(self._model, prior, self._cache, param_list, noise
                    = self._noise, head = head, tail = tail, head_args = head_args,
                    tail_args = tail_args, train_args = train_args, N = N)

            v_old = prior.volume()
            prior = posterior.gen_constr_prior(self._obs)
            v_new = prior.volume()

            if keep_history:
                self._history.append(dict(
                    posterior=posterior))

            self._posterior = posterior

            print("New prior volume:", v_new)
            print("Constrained prior volume decreased by factor", v_new/v_old)

            print("Done!", v_old, v_new, vmax, vr)
            if (v_new/v_old > vr) and (v_new < vmax):
                print("Done!", v_old, v_new, vmax, vr)
                break  # Break loop if good enough
            else:
                # Increase number of training data points systematically
                N = min(int(N*(1+(f-1)*(v_new/v_old))), Nmax)

    def gen_1d_marginals(self, params = None, N = 1000, train_args={}, head=DefaultHead, tail=DefaultTail, head_args={}, tail_args={}):
        param_list = format_param_list(params, all_params = self._cache.params, mode = '1d')
        print("Generating marginals for:", param_list)
        return self.gen_custom_marginals(param_list, N = N, train_args=train_args, head=head, tail=tail, head_args=head_args, tail_args=tail_args)

    def gen_2d_marginals(self, params = None, N = 1000, train_args={}, head=DefaultHead, tail=DefaultTail, head_args={}, tail_args={}):
        param_list = format_param_list(params, all_params = self._cache.params, mode = '2d')
        print("Generating marginals for:", param_list)
        return self.gen_custom_marginals(param_list, N = N, train_args=train_args, head=head, tail=tail, head_args=head_args, tail_args=tail_args)

    def gen_custom_marginals(self, param_list, N = 1000, train_args={}, head=DefaultHead, tail=DefaultTail, head_args={}, tail_args={}):
        """Perform one round of 2-dim posterior estimation.
        
        Args:
            params (list of str): List of parameters for which inference is performed.
            ...
        """
        if self._posterior is None:
            prior = self._prior
        else:
            prior = self._posterior.prior
            print("Using volume:", prior.volume())

        param_list = format_param_list(param_list, all_params = self._cache.params)

        posterior = _amortize(
                        model = self._model,
                        prior = prior,
                        noise = self._noise,
                        cache = self._cache,
                        N = N,
                        param_list = param_list,
                        head = head,
                        tail = tail,
                        head_args = head_args,
                        tail_args = tail_args,
                        train_args = train_args
                      )
        return posterior
        
    def cache(self):
        return self._cache

    def state_dict(self):
        return dict(
                prior = self._prior,
                posterior = self._posterior,
                obs = self._obs,
                )

    @classmethod
    def from_state_dict(cls, state_dict, model, noise = None, cache = None, device = 'cpu'):
        prior = Prior.from_state_dict(state_dict['prior'])
        posterior = Posterior.from_state_dict(state_dict['posterior'])
        obs = state_dict['obs']

        nr = NestedRatios(model, prior, obs, noise = noise, cache = cache, device = device)
        nr._posterior = posterior
        return nr
