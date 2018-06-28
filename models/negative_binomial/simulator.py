import abc

import numpy as np

import utils.random as rand_utils
from models.base import BasicSimulator

from .base import Model


class Simulator(Model, BasicSimulator, metaclass=abc.ABCMeta):

    @property
    def sample_data(self):
        return self.data["sample_data"].values

    @property
    def mu(self):
        return np.tile(self.params['mu'], (self.num_samples, 1))

    @property
    def r(self):
        return np.tile(self.params['r'], (self.num_samples, 1))

    def generate_params(self, *args, min_mean=200, max_mean=100000, min_r=10, max_r=100, **kwargs):
        """
        
        :param min_mean: minimum mean value
        :param max_mean: maximum mean value
        :param min_r: minimum r value
        :param max_r: maximum r value
        """

        mean = np.random.uniform(min_mean, max_mean, [self.num_genes])
        r = np.round(np.random.uniform(min_r, max_r, [self.num_genes]))

        # dist = rand_utils.NegativeBinomial(
        #     mean=mean,
        #     r=r
        # )

        self.params["mu"] = ("genes", mean)
        self.params["r"] = ("genes", r)

    def generate_data(self):
        self.data["sample_data"] = (
            ("samples", "genes"),
            rand_utils.NegativeBinomial(mean=self.mu, r=self.r).sample()
        )


def sim_test():
    sim = Simulator()
    sim.generate()
    sim.save("unit_test.h5")
    return sim
