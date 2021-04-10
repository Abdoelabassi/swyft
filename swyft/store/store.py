# pylint: disable=no-member, not-callable
import enum
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import namedtuple
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union
from warnings import warn

import fasteners
import numcodecs
import numpy as np
import zarr

from swyft.store.exceptions import LowIntensityError
from swyft.types import Array, PathType, Shape
from swyft.utils import all_finite, is_empty
from swyft.marginals.prior import Prior


class SimulationStatus(enum.IntEnum):
    PENDING = 0
    RUNNING = 1
    FINISHED = 2
    FAILED = 2


class Filesystem:
    metadata = "metadata"
    log_lambdas = "metadata/log_lambdas"
    samples = "samples"
    sims = "samples/sims"
    pars = "samples/pars"
    log_w = "samples/log_w"
    simulation_status = "samples/simulation_status"
    #failed_simulation = "samples/failed_simulation"
    #which_intensity = "samples/which_intensity"


class Store(ABC):
    """Abstract base class for various stores."""

    _filesystem = Filesystem

    @abstractmethod
    def __init__(
        self,
        params: Union[int, list],
        store: Union[zarr.MemoryStore, zarr.DirectoryStore],
        simulator = None,
        sync_path: Optional[PathType] = None,
    ):
        """Initialize Store content dimensions.

        Args:
            params (list of strings or int): List of paramater names.  If int use ['z0', 'z1', ...].
            store: zarr storage.
            sync_path: path to the cache lock files. Must be accessible to all
                processes working on the cache.
        """
        self._store = store
        self._simulator = simulator

        if isinstance(params, int):
            params = ['z%i'%i for i in range(params)]
        self.params = params

        synchronizer = zarr.ProcessSynchronizer(sync_path) if sync_path else None
        self._root = zarr.group(store=self.store, synchronizer=synchronizer)

        logging.debug("  params = %s" % str(params))

        if set(["samples", "metadata"]) == set(self._root.keys()):
            logging.info("Loading existing store.")
            self._update()
        elif len(self._root.keys()) == 0:
            logging.info("Creating new store.")
            self._setup_new_store(len(self.params), simulator.sim_shapes, self._root)
            logging.debug("  sim_shapes = %s" % str(simulator.sim_shapes))
        else:
            raise KeyError(
                "The zarr storage is corrupted. It should either be empty or only have the keys ['samples', 'metadata']."
            )

        self._lock = None
        if sync_path is not None:
            self._setup_lock(sync_path)

    @property
    def store(self):
        return self._store

    def _setup_lock(self, sync_path):
        path = os.path.join(sync_path, "cache.lock")
        self._lock = fasteners.InterProcessLock(path)

    def lock(self):
        if self._lock is not None:
            logging.debug("Cache locked")
            self._lock.acquire(blocking=True)

    def unlock(self):
        if self._lock is not None:
            self._lock.release()
            logging.debug("Cache unlocked")

    def _setup_new_store(self, zdim, sim_shapes, root) -> None: # Adding observational shapes to store
        # Parameters
        root.zeros(  # noqa: F841
            self._filesystem.pars,
            shape=(0, zdim),
            chunks=(100000, 1),
            dtype="f8",
        )

        # Simulations
        sims = root.create_group(self._filesystem.sims)
        for name, shape in sim_shapes.items():
            sims.zeros(name, shape=(0, *shape), chunks=(1, *shape), dtype="f8")

        # Random intensity weights
        root.zeros(  # noqa: F841
            self._filesystem.log_w,
            shape=(0,),
            chunks=(100000,),
            dtype="f8",
        )

        # Pickled Intensity (prior * N) objects
        root.create(  # noqa: F841
            self._filesystem.log_lambdas,
            shape=(0,),
            dtype=object,
            object_codec=numcodecs.Pickle(),
        )

        # Simulation status code
        root.zeros(  # noqa: F841
            self._filesystem.simulation_status,
            shape=(0,),
            chunks=(100000,),
            dtype="int",
        )

#        # Failed simulation flag
#        f = root.zeros(  # noqa: F841
#            self._filesystem.failed_simulation,
#            shape=(0,),
#            chunks=(100000,),
#            dtype="bool",
#        )

#        # Which intensity flag
#        wu = self._root.zeros(  # noqa: F841
#            self._filesystem.which_intensity,
#            shape=(0,),
#            chunks=(100000,),
#            dtype="i4",
#        )


    @staticmethod
    def _extract_xshape_from_zarr_group(group):
        return group[Store._filesystem.sims].shape[1:]

    @staticmethod
    def _extract_zdim_from_zarr_group(group):
        return group[Store._filesystem.par].shape[1]

    @staticmethod
    def _extract_sim_shapes_from_zarr_group(group):
        return {k:v.shape[1:] for k,v in group[Cache._filesystem.sims].items()}

    @staticmethod
    def _extract_params_from_zarr_group(group):
        return [k for k in group[Store._filesystem.pars].keys()]

    def _update(self):
        self.sims = self._root[self._filesystem.sims]
        self.pars = self._root[self._filesystem.pars]
        self.log_w = self._root[self._filesystem.log_w]
        self.log_lambdas = self._root[self._filesystem.log_lambdas]
        self.sim_status = self._root[self._filesystem.simulation_status]

        #self.f = self._root[self._filesystem.failed_simulation]
        #assert len(self.f) == len(
        #    self.m
        #), "Metadata noting which indices require simulation and which have failed have desynced."
        #self.wu = self._root[self._filesystem.which_intensity]

    def __len__(self):
        """Returns number of samples in the store."""
        self._update()
        return len(self.pars)

    def __getitem__(self, i):
        self._update()

        result_x = {}
        for key, value in self.sims.items():
            result_x[key] = value[i]

        result_z = self.pars[i]

        return dict(x=result_x, z=result_z)

    def _append_z(self, z, log_w):
        """Append z to store content and generate new slots for x."""
        self._update()

        # Length of first element
        n = len(z)
        #n = len(z[list(z)[0]])

        # Add slots for x
        for key, value in self.sims.items():
            shape = list(value.shape)
            shape[0] += n
            value.resize(*shape)

        self._root[self._filesystem.pars].append(z)
        self._root[self._filesystem.log_w].append(log_w)
        #for key, value in self.pars.items():
        #    value.append(z[key])

        # Register as pending
        m = np.full(n, SimulationStatus.PENDING, dtype="int")
        self.sim_status.append(m)

#        # Simulations have not failed, yet.
#        f = np.zeros(n, dtype="bool")
#        self.f.append(f)

        ## Which intensity was a parameter drawn with
        #wu = self.intensity_len * np.ones(n, dtype="int")
        #self.wu.append(wu)

    def log_lambda(self, z: np.ndarray) -> np.ndarray:
        self._update()
        d = -np.inf * np.ones_like(z[:,0])
        if len(self.log_lambdas) == 0:
            return d
        for i in range(len(self.log_lambdas)):
            pdf = Prior.from_state_dict(self.log_lambdas[i]['pdf'])
            N = self.log_lambdas[i]['N']
            r = pdf.log_prob(z) + np.log(N)
            d = np.where(r > d, r, d)
        return d

    def sample(self, N, pdf):
        self._update()

        # Generate new points
        z_prop = pdf.sample(N = np.random.poisson(N))
        log_lambda_target = pdf.log_prob(z_prop) + np.log(N)
        log_lambda_store = self.log_lambda(z_prop)
        log_w = np.log(np.random.rand(len(z_prop))) + log_lambda_target
        accept_new = log_w > log_lambda_store
        z_new = z_prop[accept_new]
        log_w_new = log_w[accept_new]

        # Anything new?
        if sum(accept_new) > 0:
            # Add new entries to store
            self._append_z(z_new, log_w_new)
            logging.info("  adding %i new samples to simulator store." % sum(accept_new))
            # Update intensity function
            self.log_lambdas.resize(len(self.log_lambdas) + 1)
            self.log_lambdas[-1] = dict(pdf = pdf.state_dict(), N = N)

#        self.lock()
        self._update()
#        self.unlock()

        # Select points from cache
        z_store = self.pars
        log_w_store = self.log_w
        log_lambda_target = pdf.log_prob(z_store) + np.log(N)
        accept_stored = log_w_store < log_lambda_target
        indices = np.array(range(len(accept_stored)))[accept_stored]

        return indices

    def _get_indices_to_simulate(self, indices=None):
        """
        Determine which samples need to be simulated.

        Args:
            indices: (optional) array with the indices of the samples to
            consider. If None, consider all samples.

        Returns:
            array with the sample indices
        """
        status = self.get_simulation_status(indices)
        require_simulation = status == SimulationStatus.PENDING
        idx = np.flatnonzero(require_simulation)
        return indices[idx] if indices is not None else idx

    def _set_simulation_status(self, indices, status):
        """
        Flag the specified samples with the simulation status.

        Args:
            indices: array with the indices of the samples to flag
            status: new status for the samples
        """
        assert status in list(SimulationStatus), f"Unknown status {status}"
        current_status = self.sim_status.oindex[indices]
        if np.any(current_status == status):
            raise ValueError(f"Some simulations have already status {status}")
        self.sim_status.oindex[indices] = status

    def get_simulation_status(self, indices=None):
        """
        Determine the status of sample simulations.

        Args:
            indices: list of indices. If None, check the status of all samples

        Returns:
            list of simulation statuses
        """
        self._update()
        return self.sim_status.oindex[indices] if indices is not None else self.sim_status[:]

    def requires_sim(self, indices = None) -> bool:
        """Check whether there are parameters which require a matching simulation."""
        self._update()
        return self._get_indices_to_simulate(indices).size > 0

    def _get_indices_failed_simulations(self):
        return np.flatnonzero(self.f[:])

    @property
    def any_failed(self) -> bool:
        """Check whether there are parameters which currently lead to a failed simulation."""
        self._update()
        return self._get_indices_failed_simulations().size > 0

    def _add_sim(self, i, x):
        for k, v in x.items():
            self.sims[k][i] = v
        self._set_simulation_status(i, SimulationStatus.FINISHED)
        #self.f[i] = False

#    def _failed_sim(self, i):
#        self.f[i] = True

    def _replace(self, i, z, x):
        for key, value in z.items():
            self.pars[key][i] = value
        for k, v in x.items():
            self.sims[k][i] = v
        self._set_simulation_status(i, SimulationStatus.FINISHED)
#        self.f[i] = False

    @staticmethod
    def did_simulator_succeed(x: Dict[str, Array], fail_on_non_finite: bool) -> bool:
        """Is the simulation a success?"""

        assert isinstance(x, dict), "Simulators must return a dictionary."

        dict_anynone = lambda d: any(v is None for v in d.values())

        if dict_anynone(x):
            return False
        elif fail_on_non_finite and not all_finite(x):
            return False
        else:
            return True

    def set_simulator(self, simulator):
        self._simulator = simulator

    def simulate(
        self,
        indices: Optional[List[int]] = None,
        fail_on_non_finite: bool = True,
#        max_attempts: int = 1000,
    ) -> None:
        """Run simulator sequentially on parameter store with missing corresponding simulations.

        Args:
            simulator: simulates an observation given a parameter input
            indices: list of sample indices for which a simulation is required
            fail_on_non_finite: if nan / inf in simulation, considered a failed simulation
        """
        self.lock()
        idx = self._get_indices_to_simulate(indices)
        self._set_simulation_status(idx, SimulationStatus.RUNNING)
        self.unlock()

        if self._simulator is None:
            logging.warning("No simulator specified")
            return
        else:
            simulator = self._simulator

        if len(idx) == 0:
            logging.debug("No simulations required.")
            return
        else:
            z = [self.pars[i] for i in idx]
            res = simulator.run(z)
            x_all, validity = list(zip(*res))  # TODO: check other data formats

            for i, x, v in zip(idx, x_all, validity):
                if v == 0:
                    self._add_sim(i, x)
                else:
                    self._failed_sim(i)

        # some of the samples might be run by other processes - wait for these
        self.wait_for_simulations(indices)

#        for i in tqdm(idx, desc="Simulate"):
#            x = simulator(z)
#            success = self.did_simulator_succeed(x, fail_on_non_finite)
#            if success:
#                self._add_sim(i, x)
#            else:
#                self._failed_sim(i)
#
#        if self.any_failed:
#            self.resample_failed_simulations(
#                simulator, fail_on_non_finite, max_attempts
#            )
#            # TODO add test which ensures that volume does not change upon more samples.
#
#        if self.any_failed:
#            warn(
#                f"Some simulations failed, despite {max_attempts} to resample them. They have been marked in the store."
#            )

    def wait_for_simulations(self, indices):
        """
        Wait for a set of sample simulations to be finished.

        Args:
            indices: list of sample indices
        """
        status = self.get_simulation_status(indices)
        while not np.all(status == SimulationStatus.FINISHED):
            time.sleep(1)
            status = self.get_simulation_status(indices)

class DirectoryStore(Store):
    def __init__(
        self,
        params,
        path: PathType,
        sync_path: Optional[PathType] = None,
        simulator = None,
    ):
        """Instantiate an iP3 store stored in a directory.

        Args:
            zdim: Number of z dimensions
            sim_shapes: Shape of x array
            path: path to storage directory
            sync_path: path to the cache lock files. Must be accessible to all
                processes working on the cache and should differ from `path`.
        """
        store = zarr.DirectoryStore(path)
        sync_path = sync_path or os.path.splitext(path)[0] + ".sync"
        super().__init__(
            params=params, store=store, sync_path=sync_path, simulator=simulator
        )

    @classmethod
    def load(cls, path: PathType):
        """Load existing DirectoryStore."""
        store = zarr.DirectoryStore(path)
        group = zarr.group(store=store)
        xshape = cls._extract_xshape_from_zarr_group(group)
        zdim = cls._extract_zdim_from_zarr_group(group)
        return DirectoryStore(zdim=zdim, xshape=xshape, path=path)

        #sim_shapes = cls._extract_sim_shapes_from_zarr_group(group)
        #z = cls._extract_params_from_zarr_group(group)
        #return DirectoryCache(params=z, sim_shapes=sim_shapes, path=path)


class MemoryStore(Store):
    def __init__(
        self,
        params,
        store=None,
        simulator=None,
        sync_path: Optional[PathType] = None,
    ):
        """Instantiate an iP3 store stored in the memory.

        Args:
            zdim: Number of z dimensions
            store (zarr.MemoryStore, zarr.DirectoryStore): optional, used in
                loading.
        """
        if store is None:
            store = zarr.MemoryStore()
            logging.debug("Creating new empty MemoryStore.")
        else:
            logging.debug("Creating MemoryStore from store.")
        super().__init__(params=params, store=store, simulator=simulator, sync_path=sync_path)

    def save(self, path: PathType) -> None:
        """Save the current state of the MemoryStore to a directory."""
        path = Path(path)
        if path.exists() and not path.is_dir():
            raise NotADirectoryError(f"{path} should be a directory")
        elif path.exists() and not is_empty(path):
            raise FileExistsError(f"{path} is not empty")
        else:
            path.mkdir(parents=True, exist_ok=True)
            store = zarr.DirectoryStore(path)
            zarr.convenience.copy_store(source=self.store, dest=store)
            return None

    @classmethod
    def load(cls, path: PathType):
        """Load existing DirectoryStore state into a MemoryStore object."""
        memory_store = zarr.MemoryStore()
        directory_store = zarr.DirectoryStore(path)
        zarr.convenience.copy_store(source=directory_store, dest=memory_store)

        group = zarr.group(store=memory_store)
        xshape = cls._extract_xshape_from_zarr_group(group)
        zdim = cls._extract_zdim_from_zarr_group(group)
        return MemoryStore(zdim=zdim, xshape=xshape, store=memory_store)
        #sim_shapes = cls._extract_sim_shapes_from_zarr_group(group)
        #z = cls._extract_params_from_zarr_group(group)
        #return MemoryCache(params=z, sim_shapes=sim_shapes, store=memory_store)

    @classmethod
    def from_simulator(cls, model, prior):
        """Convenience function to instantiate new MemoryStore with correct sim_shapes.

        Args:
            model (function): Simulator model.
            prior (Prior): Model prior.

        Note:
            The simulator model is run once in order to infer observable shapes from the output.
        """
        v = prior.sample(1)[0]
        vdim = len(v)

        sim = model(v)

        sim_shapes = {k: v.shape for k, v in sim.items()}

        return MemoryStore(vdim, sim_shapes)
