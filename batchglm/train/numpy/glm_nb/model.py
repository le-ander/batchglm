import logging
import numpy as np

from .external import Model, ModelGlm, InputDataGLM
from .processModel import ProcessModel

logger = logging.getLogger(__name__)


class ModelIwls(ModelGlm, Model, ProcessModel):

    compute_mu: bool
    compute_r: bool

    def __init__(
            self,
            input_data: InputDataGLM,
            model_vars,
            compute_mu,
            compute_r,
            dtype,
    ):
        self.compute_mu = compute_mu
        self.compute_r = compute_r

        Model.__init__(
            self=self,
            input_data=input_data
        )
        ModelGlm.__init__(
            self=self,
            model_vars=model_vars
        )

    @property
    def fim_weight(self):
        return np.multiply(
            self.location,
            np.divide(self.scale, self.scale + self.location)
        )

    @property
    def ll(self):
        log_r_plus_mu = np.log(self.scale + self.location)
        ll = np.lgamma(self.scale + self.x) - \
            np.lgamma(self.x + 1.) - \
            np.lgamma(self.scale) + \
            np.multiply(X, self.eta_loc - log_r_plus_mu) + \
            np.multiply(self.scale, self.eta_scale - log_r_plus_mu)
        return self.np_clip_param(ll, "ll")
