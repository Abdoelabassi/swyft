from importlib import import_module
from typing import Callable, TypeVar

import numpy as np
import torch
from toolz import compose
from toolz.dicttoolz import keyfilter
from torch.distributions import Normal, Uniform

from swyft.bounds import Bound, UnitCubeBound
from swyft.types import PathType
from swyft.utils import array_to_tensor, tensor_to_array

PriorType = TypeVar("PriorType", bound="Prior")


class PriorTruncator:
    """Samples from a truncated version of the prior and calculates the log_prob.

    Args:
        prior: Parameter prior
        bound: Bound object

    .. note::
        The prior truncator is defined through a swyft.Bound object, which
        sample from (subregions of) the hypercube, with swyft.Prior, which maps
        the samples onto parameters of interest.
    """

    def __init__(self, prior: "Prior", bound: Bound) -> None:
        """Instantiate prior truncator (combination of prior and bound).

        Args:
            prior: Prior object.
            bound: Bound on hypercube.  Set 'None' for untruncated priors.
        """
        self.prior = prior
        if bound is None:
            bound = UnitCubeBound(prior.n_parameters)
        self.bound = bound

    def sample(self, N: int) -> np.ndarray:
        """Sample from truncated prior.

        Args:
            N: Number of samples to return

        Returns:
            Samples: (N, n_parameters)
        """
        u = self.bound.sample(N)
        return self.prior.v(u)

    def log_prob(self, v: np.ndarray) -> np.ndarray:
        """Evaluate log probability.

        Args:
            v: (N, n_parameters) parameter points.

        Returns:
            log_prob: (N,)
        """
        u = self.prior.u(v)
        b = np.where(u.sum(axis=-1) == np.inf, 0.0, self.bound(u))
        log_prob = np.where(
            b == 0.0,
            -np.inf,
            self.prior.log_prob(v).sum(axis=-1) - np.log(self.bound.volume),
        )
        return log_prob

    def state_dict(self) -> dict:
        return dict(prior=self.prior.state_dict(), bound=self.bound.state_dict())

    @classmethod
    def from_state_dict(cls, state_dict: dict):
        prior = Prior.from_state_dict(state_dict["prior"])
        bound = Bound.from_state_dict(state_dict["bound"])
        return cls(prior, bound)

    @classmethod
    def load(cls, filename: PathType):
        sd = torch.load(filename)
        return cls.from_state_dict(sd)

    def save(self, filename: PathType) -> None:
        sd = self.state_dict()
        torch.save(sd, filename)


class InterpolatedTabulatedDistribution:
    def __init__(self, icdf: Callable, n_parameters: int, n_grid_points: int) -> None:
        r"""Create a distribution based off of a icdf. The distribution is defined by interpolating grid points.

        Args:
            icdf: inverse cumulative density function, aka ppf and uv
            n_parameters: number of parameters, dimensionality of the prior
            n_grid_points: number of grid points

        .. warning::
            Internally the mapping u -> v is tabulated on a linear grid on the
            interval [0, 1], with `n` grid points. In extreme cases, this can
            lead to approximation errors that can be mitigated by increasing
            `n`.
        """
        self.n_parameters = n_parameters
        self._grid = np.linspace(0, 1.0, n_grid_points)
        self._table = self._generate_table(icdf, self._grid, n_parameters)

    @staticmethod
    def _generate_table(
        uv: Callable, grid: np.ndarray, n_parameters: int
    ) -> np.ndarray:
        table = []
        for x in grid:
            table.append(uv(np.ones(n_parameters) * x))
        return np.array(table).T

    def u(self, v: np.ndarray) -> np.ndarray:
        """Map onto hypercube: v -> u

        Args:
            v: (N, n_parameters) physical parameter array

        Returns:
            u: (N, n_parameters) hypercube parameter array
        """
        u = np.empty_like(v)
        for i in range(self.n_parameters):
            u[:, i] = np.interp(
                v[:, i], self._table[i], self._grid, left=np.inf, right=np.inf
            )
        return u

    def v(self, u: np.ndarray) -> np.ndarray:
        """Map from hypercube: u -> v

        Args:
            u: (N, n_parameters) hypercube parameter array

        Returns:
            v: (N, n_parameters) physical parameter array
        """
        v = np.empty_like(u)
        for i in range(self.n_parameters):
            v[:, i] = np.interp(
                u[:, i], self._grid, self._table[i], left=np.inf, right=np.inf
            )
        return v

    def log_prob(self, v: np.ndarray, du: float = 1e-6) -> np.ndarray:
        """Log probability.

        Args:
            v: (N, n_parameters) physical parameter array
            du: Step-size of numerical derivatives

        Returns:
            log_prob: (N, n_parameters) factors of pdf
        """
        dv = np.empty_like(v)
        u = self.u(v)
        for i in range(self.n_parameters):
            dv[:, i] = np.interp(
                u[:, i] + (du / 2), self._grid, self._table[i], left=None, right=None
            )
            dv[:, i] -= np.interp(
                u[:, i] - (du / 2), self._grid, self._table[i], left=None, right=None
            )
        log_prob = np.where(u == np.inf, -np.inf, np.log(du) - np.log(dv + 1e-300))
        return log_prob


# TODO this could be improved with some thought
# it merely wraps a torch distribution and keeps track of the arguments...
class Prior:
    def __init__(
        self, cdf: Callable, icdf: Callable, log_prob: Callable, n_parameters: int
    ) -> None:
        r"""Fully factorizable prior.

        Args:
            cdf: cumulative density function, aka vu
            icdf: inverse cumulative density function, aka ppf and uv
            log_prob: log density function
            n_parameters: number of parameters / dimensionality of the prior

        .. note::
            The prior is defined through the mapping :math:`u\to v`, from the
            Uniform distribution, :math:`u\sim \text{Unif}(0, 1)` onto the
            parameters of interest, :math:`v`.  This mapping corresponds to the
            inverse cummulative distribution function, and is internally used
            to perform inverse transform sampling.  Sampling happens in the
            swyft.Bound object.
        """
        self.cdf = cdf
        self.icdf = icdf
        self.log_prob = log_prob
        self.n_parameters = n_parameters
        self.method = "__init__"
        self._state_dict = {
            "method": self.method,
            "cdf": self.cdf,
            "icdf": self.icdf,
            "log_prob": self.log_prob,
            "n_parameters": self.n_parameters,
        }
        self.distribution = None

    def u(self, v: np.ndarray) -> np.ndarray:
        """Map onto hypercube: v -> u. cumulative density function (cdf)

        Args:
            v: (N, n_parameters) batched physical parameter array

        Returns:
            u: (N, n_parameters) batched hypercube parameter array
        """
        return self.cdf(v)

    def v(self, u: np.ndarray) -> np.ndarray:
        """Map from hypercube: u -> v. inverse cumulative density function (icdf)

        Args:
            u: (N, n_parameters) batched hypercube parameter array

        Returns:
            v: (N, n_parameters) batched physical parameter array
        """
        return self.icdf(u)

    @classmethod
    def from_torch_distribution(
        cls, distribution: torch.distributions.Distribution
    ) -> PriorType:
        r"""Create a prior from a batched pytorch distribution.

        For example, ``distribution = torch.distributions.Uniform(-1 * torch.ones(5), 1 * torch.ones(5))``.

        Args:
            distribution: pytorch distribution

        Returns:
            Prior
        """
        assert (
            len(distribution.batch_shape) == 1
        ), f"{distribution.batch_shape=} must be one dimensional"
        assert (
            len(distribution.event_shape) == 0
        ), f"{distribution} must be factorizable and report the log_prob of every dimension (i.e. all dims are in batch_shape)"
        prior = cls(
            cdf=compose(tensor_to_array, distribution.cdf, array_to_tensor),
            icdf=compose(tensor_to_array, distribution.icdf, array_to_tensor),
            log_prob=compose(tensor_to_array, distribution.log_prob, array_to_tensor),
            n_parameters=distribution.batch_shape.numel(),
        )
        prior.distribution = distribution
        prior.method = "from_torch_distribution"
        prior._state_dict = {
            "method": prior.method,
            "name": distribution.__class__.__name__,
            "module": distribution.__module__,
            "kwargs": keyfilter(
                lambda x: x in distribution.__class__.arg_constraints,
                distribution.__dict__,  # this depends on all relevant arguments being contained with prior.distribution.__class__.arg_constraints
            ),
        }
        return prior

    @classmethod
    def from_uv(
        cls, icdf: Callable, n_parameters: int, n_grid_points: int = 10_000
    ) -> PriorType:
        """Create a prior which depends on ``InterpolatedTabulatedDistribution``, i.e. an interpolated representation of the icdf, cdf, and log_prob.

        .. warning::
            Internally the mapping u -> v is tabulated on a linear grid on the
            interval [0, 1], with `n` grid points. In extreme cases, this can
            lead to approximation errors that can be mitigated by increasing
            `n` (in some cases).

        Args:
            icdf: map from hypercube: u -> v. inverse cumulative density function (icdf)
            n_parameters: number of parameters / dimensionality of the prior
            n_grid_points: number of grid points from which to interpolate the icdf, cdf, and log_prob

        Returns:
            Prior
        """
        raise NotImplementedError("This was too inaccurate.")
        distribution = InterpolatedTabulatedDistribution(
            icdf, n_parameters, n_grid_points
        )
        prior = cls(
            cdf=distribution.v,
            icdf=distribution.u,
            log_prob=distribution.log_prob,
            n_parameters=n_parameters,
        )
        prior.distribution = distribution
        prior.method = "from_from_uv"
        prior.state_dict = None  # TODO, make like above.
        return prior

    def state_dict(self):
        return self._state_dict

    @classmethod
    def from_state_dict(cls, state_dict: dict) -> PriorType:
        method = state_dict["method"]

        if method == "__init__":
            kwargs = keyfilter(lambda x: x != "method", state_dict)
            return cls(**kwargs)
        elif method == "from_torch_distribution":
            name = state_dict["name"]
            module = state_dict["module"]
            kwargs = state_dict["kwargs"]
            distribution = getattr(import_module(module), name)
            distribution = distribution(**kwargs)
            return getattr(cls, method)(distribution)
        else:
            NotImplementedError()


def get_uniform_prior(low: np.ndarray, high: np.ndarray) -> Prior:
    distribution = Uniform(array_to_tensor(low), array_to_tensor(high))
    return Prior.from_torch_distribution(distribution)


def get_diagonal_normal_prior(loc: np.ndarray, scale: np.ndarray) -> Prior:
    distribution = Normal(array_to_tensor(loc), array_to_tensor(scale))
    return Prior.from_torch_distribution(distribution)


if __name__ == "__main__":
    pass
