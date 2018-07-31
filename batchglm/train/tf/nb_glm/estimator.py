import abc
from typing import Union, Dict, Tuple, List
import logging
from enum import Enum

# import xarray as xr
import tensorflow as tf
import numpy as np

try:
    import anndata
except ImportError:
    anndata = None

from .external import AbstractEstimator, XArrayEstimatorStore, InputData, Model, MonitoredTFEstimator, TFEstimatorGraph
from .external import nb_utils, tf_linreg, train_utils, op_utils, rand_utils
from .external import pkg_constants

ESTIMATOR_PARAMS = AbstractEstimator.param_shapes().copy()
ESTIMATOR_PARAMS.update({
    "batch_probs": ("batch_observations", "features"),
    "batch_log_probs": ("batch_observations", "features"),
    "batch_log_likelihood": (),
    "full_loss": (),
    "full_gradient": ("features",),
})

logger = logging.getLogger(__name__)


def param_bounds(dtype):
    if isinstance(dtype, tf.DType):
        min = dtype.min
        max = dtype.max
        dtype = dtype.as_numpy_dtype
    else:
        min = np.finfo(dtype).min
        max = np.finfo(dtype).max

    sf = dtype(2.5)
    bounds_min = {
        "a_intercept": np.log(np.nextafter(0, np.inf, dtype=dtype)) / sf,
        "a_slope": np.log(np.nextafter(0, np.inf, dtype=dtype)) / sf,
        "b_intercept": np.log(np.nextafter(0, np.inf, dtype=dtype)) / sf,
        "b_slope": np.log(np.nextafter(0, np.inf, dtype=dtype)) / sf,
        "log_mu": np.log(np.nextafter(0, np.inf, dtype=dtype)) / sf,
        "log_r": np.log(np.nextafter(0, np.inf, dtype=dtype)) / sf,
        "mu": np.nextafter(0, np.inf, dtype=dtype),
        "r": np.nextafter(0, np.inf, dtype=dtype),
        "probs": dtype(0),
        "log_probs": np.log(np.nextafter(0, np.inf, dtype=dtype)),
    }
    bounds_max = {
        "a_intercept": np.nextafter(np.log(max), -np.inf, dtype=dtype) / sf,
        "a_slope": np.nextafter(np.log(max), -np.inf, dtype=dtype) / sf,
        "b_intercept": np.nextafter(np.log(max), -np.inf, dtype=dtype) / sf,
        "b_slope": np.nextafter(np.log(max), -np.inf, dtype=dtype) / sf,
        "log_mu": np.nextafter(np.log(max), -np.inf, dtype=dtype) / sf,
        "log_r": np.nextafter(np.log(max), -np.inf, dtype=dtype) / sf,
        "mu": np.nextafter(max, -np.inf, dtype=dtype) / sf,
        "r": np.nextafter(max, -np.inf, dtype=dtype) / sf,
        "probs": dtype(1),
        "log_probs": dtype(0),
    }
    return bounds_min, bounds_max


def clip_param(param, name):
    bounds_min, bounds_max = param_bounds(param.dtype)
    return tf.clip_by_value(
        param,
        bounds_min[name],
        bounds_max[name]
    )


class BasicModelGraph:

    def __init__(self, X, design_loc, design_scale, a, b, size_factors=None):
        dist_estim = nb_utils.NegativeBinomial(mean=tf.exp(tf.gather(a, 0)),
                                               r=tf.exp(tf.gather(b, 0)),
                                               name="dist_estim")

        with tf.name_scope("mu"):
            log_mu = tf.matmul(design_loc, a, name="log_mu_obs")
            if size_factors is not None:
                log_mu = log_mu + size_factors
            log_mu = clip_param(log_mu, "log_mu")
            mu = tf.exp(log_mu)

        with tf.name_scope("r"):
            log_r = tf.matmul(design_scale, b, name="log_r_obs")
            log_r = clip_param(log_r, "log_r")
            r = tf.exp(log_r)

        dist_obs = nb_utils.NegativeBinomial(mean=mu, r=r, name="dist_obs")

        with tf.name_scope("probs"):
            probs = dist_obs.prob(X)
            probs = clip_param(probs, "probs")

        with tf.name_scope("log_probs"):
            log_probs = dist_obs.log_prob(X)
            log_probs = clip_param(log_probs, "log_probs")

        self.X = X
        self.design_loc = design_loc
        self.design_scale = design_scale

        self.dist_estim = dist_estim
        self.mu_estim = dist_estim.mean()
        self.r_estim = dist_estim.r
        self.sigma2_estim = dist_estim.variance()

        self.dist_obs = dist_obs
        self.mu = mu
        self.r = r
        self.sigma2 = dist_obs.variance()

        self.probs = probs
        self.log_probs = log_probs
        self.log_likelihood = tf.reduce_sum(self.log_probs, name="log_likelihood")

        with tf.name_scope("loss"):
            self.loss = -tf.reduce_mean(self.log_probs)


class ModelVars:
    a: tf.Tensor
    a_intercept: tf.Variable
    a_slope: tf.Variable
    b: tf.Tensor
    b_intercept: tf.Variable
    b_slope: tf.Variable

    def __init__(
            self,
            X,
            design_loc,
            design_scale,
            init_a_intercept=None,
            init_a_slopes=None,
            init_b_intercept=None,
            init_b_slopes=None,
            name="Linear_Batch_Model",
    ):
        with tf.name_scope(name):
            num_design_loc_params = design_loc.shape[-1]
            num_design_scale_params = design_scale.shape[-1]
            (batch_size, num_features) = X.shape

            assert X.shape == [batch_size, num_features]
            assert design_loc.shape == [batch_size, num_design_loc_params]
            assert design_scale.shape == [batch_size, num_design_scale_params]

            # ### parameter bounds
            dtype = X.dtype

            with tf.name_scope("initialization"):
                # implicit broadcasting of X and initial_mixture_probs to
                #   shape (num_mixtures, num_observations, num_features)
                init_dist = nb_utils.fit(X, axis=-2)
                assert init_dist.r.shape == [1, num_features]

                if init_a_intercept is None:
                    init_a_intercept = tf.log(init_dist.mean())
                else:
                    init_a_intercept = tf.convert_to_tensor(init_a_intercept, dtype=X.dtype)
                init_a_intercept = clip_param(init_a_intercept, "a_intercept")

                if init_b_intercept is None:
                    init_b_intercept = tf.log(init_dist.r)
                else:
                    init_b_intercept = tf.convert_to_tensor(init_b_intercept, dtype=X.dtype)
                init_b_intercept = clip_param(init_b_intercept, "b_intercept")

                assert init_a_intercept.shape == [1, num_features] == init_b_intercept.shape

                if init_a_slopes is None:
                    init_a_slopes = tf.random_uniform(
                        tf.TensorShape([num_design_loc_params - 1, num_features]),
                        minval=np.nextafter(0, 1, dtype=dtype.as_numpy_dtype),
                        maxval=np.sqrt(np.nextafter(0, 1, dtype=dtype.as_numpy_dtype)),
                        dtype=dtype
                    )
                else:
                    init_a_slopes = tf.convert_to_tensor(init_a_slopes, dtype=dtype)
                init_a_slopes = clip_param(init_a_slopes, "a_slope")

                if init_b_slopes is None:
                    init_b_slopes = tf.random_uniform(
                        tf.TensorShape([num_design_scale_params - 1, num_features]),
                        minval=np.nextafter(0, 1, dtype=dtype.as_numpy_dtype),
                        maxval=np.sqrt(np.nextafter(0, 1, dtype=dtype.as_numpy_dtype)),
                        dtype=dtype
                    )
                else:
                    init_b_slopes = tf.convert_to_tensor(init_b_slopes, dtype=dtype)
                init_b_slopes = clip_param(init_b_slopes, "b_slope")

            a, a_intercept, a_slope = tf_linreg.param_variable(init_a_intercept, init_a_slopes, name="a")
            b, b_intercept, b_slope = tf_linreg.param_variable(init_b_intercept, init_b_slopes, name="b")
            assert a.shape == (num_design_loc_params, num_features)
            assert b.shape == (num_design_scale_params, num_features)

            self.a = a
            self.a_intercept = a_intercept
            self.a_slope = a_slope
            self.b = b
            self.b_intercept = b_intercept
            self.b_slope = b_slope


# def fetch_batch(indices, X, design):
#     batch_X = tf.gather(X, indices)
#     batch_design = tf.gather(design, indices)
#     return indices, (batch_X, batch_design)

def feature_wise_hessians(X, design_loc, design_scale, a, b, size_factors=None) -> List[tf.Tensor]:
    X_t = tf.transpose(tf.expand_dims(X, axis=0), perm=[2, 0, 1])
    a_t = tf.transpose(tf.expand_dims(a, axis=0), perm=[2, 0, 1])
    b_t = tf.transpose(tf.expand_dims(b, axis=0), perm=[2, 0, 1])

    def hessian(data):  # data is tuple (X_t, a_t, b_t)
        X_t, a_t, b_t = data
        X = tf.transpose(X_t)
        a = tf.transpose(a_t)
        b = tf.transpose(b_t)

        model = BasicModelGraph(X, design_loc, design_scale, a, b, size_factors=size_factors)

        hess = tf.hessians(-model.log_likelihood, [a, b])

        return hess

    hessians = tf.map_fn(
        fn=hessian,
        elems=(X_t, a_t, b_t),
        dtype=[tf.float32, tf.float32],  # hessians of [a, b]
        parallel_iterations=pkg_constants.TF_LOOP_PARALLEL_ITERATIONS
    )

    stacked = [tf.squeeze(tf.squeeze(tf.stack(t), axis=2), axis=3) for t in hessians]

    return stacked


class FullDataModelGraph:
    def __init__(
            self,
            sample_indices: tf.Tensor,
            fetch_fn,
            batch_size: Union[int, tf.Tensor],
            a: tf.Tensor,
            b: tf.Tensor
    ):
        dataset = tf.data.Dataset.from_tensor_slices(sample_indices)

        batched_data = dataset.batch(batch_size)
        batched_data = batched_data.map(fetch_fn)
        batched_data = batched_data.prefetch(1)

        def map_model(idx, data) -> BasicModelGraph:
            X, design_loc, design_scale, size_factors = data
            model = BasicModelGraph(X, design_loc, design_scale, a, b, size_factors=size_factors)
            return model

        super()
        model = map_model(*fetch_fn(sample_indices))

        with tf.name_scope("log_likelihood"):
            log_likelihood = op_utils.map_reduce(
                last_elem=tf.gather(sample_indices, tf.size(sample_indices) - 1),
                data=batched_data,
                map_fn=lambda idx, data: map_model(idx, data).log_likelihood,
                parallel_iterations=1,
            )

        with tf.name_scope("loss"):
            loss = -log_likelihood / tf.cast(tf.size(sample_indices), dtype=log_likelihood.dtype)

        with tf.name_scope("hessians"):
            def hessian_map(idx, data):
                X, design_loc, design_scale, size_factors = data
                return feature_wise_hessians(X, design_loc, design_scale, a, b, size_factors=size_factors)

            def hessian_red(prev, cur):
                return [tf.add(p, c) for p, c in zip(prev, cur)]

            hessians = op_utils.map_reduce(
                last_elem=tf.gather(sample_indices, tf.size(sample_indices) - 1),
                data=batched_data,
                map_fn=hessian_map,
                reduce_fn=hessian_red,
                parallel_iterations=1,
            )

        self.X = model.X
        self.design_loc = model.design_loc
        self.design_scale = model.design_scale

        self.batched_data = batched_data

        self.dist_estim = model.dist_estim
        self.mu_estim = model.mu_estim
        self.r_estim = model.r_estim
        self.sigma2_estim = model.sigma2_estim

        self.dist_obs = model.dist_obs
        self.mu = model.mu
        self.r = model.r
        self.sigma2 = model.sigma2

        self.probs = model.probs
        self.log_probs = model.log_probs

        # custom
        self.sample_indices = sample_indices

        self.log_likelihood = log_likelihood
        self.loss = loss

        self.hessians = hessians


class EstimatorGraph(TFEstimatorGraph):
    X: tf.Tensor

    mu: tf.Tensor
    sigma2: tf.Tensor
    a: tf.Tensor
    b: tf.Tensor

    def __init__(
            self,
            fetch_fn,
            feature_isnonzero,
            num_observations,
            num_features,
            num_design_loc_params,
            num_design_scale_params,
            graph: tf.Graph = None,
            batch_size=500,
            init_a_intercept=None,
            init_a_slopes=None,
            init_b_intercept=None,
            init_b_slopes=None,
            extended_summary=False,
    ):
        super().__init__(graph)
        self.num_observations = num_observations
        self.num_features = num_features
        self.num_design_loc_params = num_design_loc_params
        self.num_design_scale_params = num_design_scale_params
        self.batch_size = batch_size

        # initial graph elements
        with self.graph.as_default():
            # ### placeholders
            learning_rate = tf.placeholder(tf.float32, shape=(), name="learning_rate")
            # train_steps = tf.placeholder(tf.int32, shape=(), name="training_steps")

            # ### performance related settings
            buffer_size = 4

            with tf.name_scope("input_pipeline"):
                data_indices = tf.data.Dataset.from_tensor_slices((
                    tf.range(num_observations, name="sample_index")
                ))
                training_data = data_indices.apply(tf.contrib.data.shuffle_and_repeat(buffer_size=2 * batch_size))
                training_data = training_data.apply(tf.contrib.data.batch_and_drop_remainder(batch_size))
                training_data = training_data.map(fetch_fn)
                training_data = training_data.prefetch(buffer_size)

                iterator = training_data.make_one_shot_iterator()

                batch_sample_index, batch_data = iterator.get_next()
                (batch_X, batch_design_loc, batch_design_scale, batch_size_factors) = batch_data

            dtype = batch_X.dtype

            # Batch model:
            #     only `batch_size` observations will be used;
            #     All per-sample variables have to be passed via `data`.
            #     Sample-independent variables (e.g. per-feature distributions) can be created inside the batch model
            batch_vars = ModelVars(
                batch_X,
                batch_design_loc,
                batch_design_scale,
                init_a_intercept=init_a_intercept,
                init_a_slopes=init_a_slopes,
                init_b_intercept=init_b_intercept,
                init_b_slopes=init_b_slopes,
            )

            batch_model = BasicModelGraph(batch_X, batch_design_loc, batch_design_scale, batch_vars.a, batch_vars.b,
                                          size_factors=batch_size_factors)

            # minimize negative log probability (log(1) = 0);
            # use the mean loss to keep a constant learning rate independently of the batch size
            loss = -tf.reduce_mean(batch_model.log_probs, name="loss")

            # ### management
            with tf.name_scope("training"):
                global_step = tf.train.get_or_create_global_step()

                # set up trainers for different selections of variables to train
                # set up multiple optimization algorithms for each trainer
                trainers = train_utils.MultiTrainer(
                    loss=loss,
                    variables=tf.trainable_variables(),
                    learning_rate=learning_rate,
                    global_step=global_step
                )
                trainers_a_only = train_utils.MultiTrainer(
                    loss=loss,
                    variables=[batch_vars.a_intercept, batch_vars.a_slope],
                    learning_rate=learning_rate,
                    global_step=global_step
                )
                trainers_b_only = train_utils.MultiTrainer(
                    loss=loss,
                    variables=[batch_vars.b_intercept, batch_vars.b_slope],
                    learning_rate=learning_rate,
                    global_step=global_step
                )

                gradient = trainers.gradient

                aggregated_gradient = tf.add_n(
                    [tf.reduce_sum(tf.abs(grad), axis=0) for (grad, var) in gradient])

            with tf.name_scope("init_op"):
                init_op = tf.global_variables_initializer()

            # ### output values:
            #       override all-zero features with lower bound coefficients
            with tf.name_scope("output"):
                bounds_min, bounds_max = param_bounds(dtype)

                param_nonzero_a = tf.tile(tf.expand_dims(feature_isnonzero, 0), [num_design_loc_params, 1])
                alt_a = tf.concat([
                    tf.tile(
                        tf.reshape(bounds_min["a_intercept"], [1, 1]),
                        [1, num_features]
                    ),
                    tf.zeros(shape=[num_design_loc_params - 1, num_features], dtype=batch_vars.a.dtype),
                ], axis=0, name="alt_a")
                a = tf.where(
                    param_nonzero_a,
                    batch_vars.a,
                    alt_a,
                    name="a"
                )

                param_nonzero_b = tf.tile(tf.expand_dims(feature_isnonzero, 0), [num_design_loc_params, 1])
                alt_b = tf.concat([
                    tf.tile(
                        tf.reshape(bounds_max["b_intercept"], [1, 1]),
                        [1, num_features]
                    ),
                    tf.zeros(shape=[num_design_scale_params - 1, num_features], dtype=batch_vars.a.dtype),
                ], axis=0, name="alt_b")
                b = tf.where(
                    param_nonzero_b,
                    batch_vars.b,
                    alt_b,
                    name="b"
                )

                # ### alternative definitions for custom observations:
                sample_selection = tf.placeholder_with_default(tf.range(num_observations),
                                                               shape=(None,),
                                                               name="sample_selection")
                full_data_model = FullDataModelGraph(
                    sample_indices=sample_selection,
                    fetch_fn=fetch_fn,
                    batch_size=batch_size * buffer_size,
                    a=a,
                    b=b,
                )
                full_gradient_a, full_gradient_b = tf.gradients(full_data_model.loss, (batch_vars.a, batch_vars.b))
                full_gradient = (
                        tf.reduce_sum(tf.abs(full_gradient_a), axis=0) +
                        tf.reduce_sum(tf.abs(full_gradient_b), axis=0)
                )

                with tf.name_scope("hessian_diagonal"):
                    hessian_diagonal = [
                        tf.map_fn(
                            # elems=tf.transpose(hess, perm=[2, 0, 1]),
                            elems=hess,
                            fn=tf.diag_part,
                            parallel_iterations=pkg_constants.TF_LOOP_PARALLEL_ITERATIONS
                        )
                        for hess in full_data_model.hessians
                    ]
                    hessian_diagonal = tf.concat(hessian_diagonal, axis=-1)

                mu = full_data_model.mu
                r = full_data_model.r
                sigma2 = full_data_model.sigma2

        self.fetch_fn = fetch_fn
        self.batch_vars = batch_vars
        self.batch_model = batch_model

        self.loss = loss

        self.trainers = trainers
        self.trainers_a_only = trainers_a_only
        self.trainers_b_only = trainers_b_only
        self.global_step = global_step

        self.gradient = aggregated_gradient
        self.plain_gradient = gradient

        # self.train_op_GD = train_op_GD
        # self.train_op_Adam = train_op_Adam
        # self.train_op_Adagrad = train_op_Adagrad
        # self.train_op_RMSProp = train_op_RMSProp
        # default train op
        self.train_op = trainers.train_op_GD
        self.train_op_a = trainers_a_only.train_op_GD
        self.train_op_b = trainers_b_only.train_op_GD

        self.init_ops = []
        self.init_op = init_op

        # # ### set up class attributes
        self.a = a
        self.b = b
        assert (self.a.shape == (num_design_loc_params, num_features))
        assert (self.b.shape == (num_design_scale_params, num_features))

        self.mu = mu
        self.r = r
        self.sigma2 = sigma2

        self.batch_probs = batch_model.probs
        self.batch_log_probs = batch_model.log_probs
        self.batch_log_likelihood = batch_model.log_likelihood

        self.sample_selection = sample_selection
        self.full_data_model = full_data_model

        self.full_gradient = full_gradient
        self.full_loss = full_data_model.loss
        self.hessian_diagonal = hessian_diagonal

        with tf.name_scope('summaries'):
            tf.summary.histogram('a_intercept', batch_vars.a_intercept)
            tf.summary.histogram('b_intercept', batch_vars.b_intercept)
            tf.summary.histogram('a_slope', batch_vars.a_slope)
            tf.summary.histogram('b_slope', batch_vars.b_slope)
            tf.summary.scalar('loss', loss)
            tf.summary.scalar('learning_rate', learning_rate)

            if extended_summary:
                tf.summary.scalar('median_ll',
                                  tf.contrib.distributions.percentile(
                                      tf.reduce_sum(batch_model.log_probs, axis=1),
                                      50.)
                                  )
                tf.summary.histogram('gradient_a', tf.gradients(loss, batch_vars.a))
                tf.summary.histogram('gradient_b', tf.gradients(loss, batch_vars.b))
                tf.summary.histogram("full_gradient", full_gradient)
                tf.summary.scalar("full_gradient_median",
                                  tf.contrib.distributions.percentile(full_gradient, 50.))
                tf.summary.scalar("full_gradient_mean", tf.reduce_mean(full_gradient))

        self.saver = tf.train.Saver()
        self.merged_summary = tf.summary.merge_all()


class Estimator(AbstractEstimator, MonitoredTFEstimator, metaclass=abc.ABCMeta):
    TrainingStrategy = MonitoredTFEstimator.TrainingStrategy

    model: EstimatorGraph
    _train_mu: bool
    _train_r: bool

    @classmethod
    def param_shapes(cls) -> dict:
        return ESTIMATOR_PARAMS

    def __init__(self,
                 input_data: InputData,
                 batch_size: int = 500,
                 init_model: Model = None,
                 graph: tf.Graph = None,
                 init_a_intercept=None,
                 init_a_slopes=None,
                 init_b_intercept=None,
                 init_b_slopes=None,
                 model: EstimatorGraph = None,
                 extended_summary=False,
                 ):
        """
        Create a new Estimator

        :param input_data: The input data
        :param batch_size: The batch size to use for minibatch SGD.
            Defaults to '500'
        :param graph: (optional) tf.Graph
        :param init_model: (optional) If provided, this model will be used to initialize this Estimator.
        :param init_a_intercept: (Optional) Low-level initial values for the a intercept
        :param init_a_slopes: (Optional) Low-level initial values for the a slopes
        :param init_b_intercept: (Optional) Low-level initial values for the b intercept
        :param init_b_slopes: (Optional) Low-level initial values for the b slopes
        :param model: (optional) EstimatorGraph to use. Basically for debugging.
        :param extended_summary: Include detailed information in the summaries.
            Will drastically increase runtime of summary writer, use only for debugging.
        """
        if np.any(input_data.design_loc[:, 0] != 1):
            raise ValueError("First column in design_loc has to be the interceptand therefore == '1'")
        if np.any(input_data.design_scale[:, 0] != 1):
            raise ValueError("First column in design_scale has to be the interceptand therefore == '1'")

        # ### initialization
        if model is None:
            if graph is None:
                graph = tf.Graph()

            self._input_data = input_data
            self._train_mu = True
            self._train_r = True

            if init_model is not None:
                # location
                my_loc_names = set(input_data.design_loc_names.values)
                my_loc_names = my_loc_names.intersection(init_model.input_data.design_loc_names.values)

                init_loc = np.random.uniform(
                    low=np.nextafter(0, 1, dtype=input_data.X.dtype),
                    high=np.sqrt(np.nextafter(0, 1, dtype=input_data.X.dtype)),
                    size=(input_data.num_design_loc_params, input_data.num_features)
                )
                for parm in my_loc_names:
                    init_idx = np.where(init_model.input_data.design_loc_names == parm)
                    my_idx = np.where(input_data.design_loc_names == parm)
                    init_loc[my_idx] = init_model.par_link_loc[init_idx]

                # scale
                my_scale_names = set(input_data.design_scale_names.values)
                my_scale_names = my_scale_names.intersection(init_model.input_data.design_scale_names.values)

                init_scale = np.random.uniform(
                    low=np.nextafter(0, 1, dtype=input_data.X.dtype),
                    high=np.sqrt(np.nextafter(0, 1, dtype=input_data.X.dtype)),
                    size=(input_data.num_design_scale_params, input_data.num_features)
                )
                for parm in my_scale_names:
                    init_idx = np.where(init_model.input_data.design_scale_names == parm)
                    my_idx = np.where(input_data.design_scale_names == parm)
                    init_scale[my_idx] = init_model.par_link_loc[init_idx]

                if init_a_intercept is None:
                    init_a_intercept = init_loc[[0]]
                if init_a_slopes is None:
                    init_a_slopes = init_loc[1:]
                if init_b_intercept is None:
                    init_b_intercept = init_scale[[0]]
                if init_b_slopes is None:
                    init_b_slopes = init_scale[1:]

            """
            Initialize with Maximum Likelihood / Maximum of Momentum estimators, if the design matrix
            is not confounded.

            Idea:
            $$
            mu  = exp(a) = exp(I*a) \\
                = exp[(design*design^{-1})*a] \\
                = exp[design * (design^{-1} * a)] \\
                = exp(design * a') = mu
            $$
            """
            if init_a_intercept is None and init_a_slopes is None:
                try:
                    unique_design, inverse_idx = np.unique(input_data.design_loc, axis=0, return_inverse=True)
                    inv_design = np.linalg.inv(unique_design)
                    X = input_data.X.assign_coords(group=(("observations",), inverse_idx))
                    mean = X.groupby("group").mean(dim="observations")

                    a = np.log(mean)
                    # a = a * np.eye(np.size(a))
                    a_prime = np.matmul(inv_design, a)
                    init_a_intercept = a_prime[[0]]
                    init_a_slopes = a_prime[1:]

                    self._train_mu = False
                    logger.info("Using closed-form MLE initialization for mean")
                    logger.debug("inverse design_loc matrix:\n%s", inv_design)
                except np.linalg.LinAlgError:
                    pass

            if init_b_intercept is None and init_b_slopes is None:
                try:
                    unique_design, inverse_idx = np.unique(input_data.design_scale, axis=0, return_inverse=True)
                    inv_design = np.linalg.inv(unique_design)
                    X = input_data.X.assign_coords(group=(("observations",), inverse_idx))
                    dist = rand_utils.NegativeBinomial.mme(X.groupby("group"))

                    b = np.log(dist.r)
                    # b = b * np.eye(np.size(b))
                    b_prime = np.matmul(inv_design, b)
                    init_b_intercept = b_prime[[0]]
                    init_b_slopes = b_prime[1:]

                    self._train_r = True
                    logger.info("Using closed-form MME initialization for dispersion")
                    logger.debug("inverse design_scale matrix:\n%s", inv_design)
                except np.linalg.LinAlgError:
                    pass

        # ### prepare fetch_fn:
        def fetch_fn(idx):
            X_tensor = tf.py_func(input_data.fetch_X, [idx], tf.float32)
            X_tensor.set_shape(
                idx.get_shape().as_list() + [input_data.num_features]
            )

            design_loc_tensor = tf.py_func(input_data.fetch_design_loc, [idx], tf.float32)
            design_loc_tensor.set_shape(
                idx.get_shape().as_list() + [input_data.num_design_loc_params]
            )
            design_scale_tensor = tf.py_func(input_data.fetch_design_scale, [idx], tf.float32)
            design_scale_tensor.set_shape(
                idx.get_shape().as_list() + [input_data.num_design_scale_params]
            )

            if input_data.size_factors is not None:
                size_factors_tensor = tf.log(tf.py_func(input_data.fetch_size_factors, [idx], tf.float32))
                size_factors_tensor.set_shape(idx.get_shape())
            else:
                size_factors_tensor = tf.constant(0, shape=(), dtype=X_tensor.dtype)

            # return idx, data
            return idx, (X_tensor, design_loc_tensor, design_scale_tensor, size_factors_tensor)

        with graph.as_default():
            # create model
            model = EstimatorGraph(
                fetch_fn=fetch_fn,
                feature_isnonzero=input_data.feature_isnonzero,
                num_observations=input_data.num_observations,
                num_features=input_data.num_features,
                num_design_loc_params=input_data.num_design_loc_params,
                num_design_scale_params=input_data.num_design_scale_params,
                batch_size=batch_size,
                graph=graph,
                init_a_intercept=init_a_intercept,
                init_a_slopes=init_a_slopes,
                init_b_intercept=init_b_intercept,
                init_b_slopes=init_b_slopes,
                extended_summary=extended_summary
            )

        MonitoredTFEstimator.__init__(self, model)

    def _scaffold(self):
        with self.model.graph.as_default():
            scaffold = tf.train.Scaffold(
                init_op=self.model.init_op,
                summary_op=self.model.merged_summary,
                saver=self.model.saver,
            )
        return scaffold

    def train(self, *args,
              learning_rate=0.5,
              convergence_criteria="t_test",
              loss_window_size=100,
              stop_at_loss_change=0.05,
              train_mu: bool = None,
              train_r: bool = None,
              optim_algo="gradient_descent",
              **kwargs):
        """
        Starts training of the model

        :param feed_dict: dict of values which will be feeded each `session.run()`

            See also feed_dict parameter of `session.run()`.
        :param learning_rate: learning rate used for optimization
        :param convergence_criteria: criteria after which the training will be interrupted.
            Currently implemented criterias:

            - "simple":
              stop, when `loss(step=i) - loss(step=i-1)` < `stop_at_loss_change`
            - "moving_average":
              stop, when `mean_loss(steps=[i-2N..i-N) - mean_loss(steps=[i-N..i)` < `stop_at_loss_change`
            - "absolute_moving_average":
              stop, when `|mean_loss(steps=[i-2N..i-N) - mean_loss(steps=[i-N..i)|` < `stop_at_loss_change`
            - "t_test" (recommended):
              Perform t-Test between the last [i-2N..i-N] and [i-N..i] losses.
              Stop if P("both distributions are equal") > `stop_at_loss_change`.
        :param stop_at_loss_change: Additional parameter for convergence criteria.

            See parameter `convergence_criteria` for exact meaning
        :param loss_window_size: specifies `N` in `convergence_criteria`.
        :param train_mu: Set to True/False in order to enable/disable training of mu
        :param train_r: Set to True/False in order to enable/disable training of r
        :param optim_algo: name of the requested train op. Can be:
        
            - "Adam"
            - "Adagrad"
            - "RMSprop"
            - "GradientDescent" or "GD"
            
            See :func:train_utils.MultiTrainer.train_op_by_name for further details.
        """
        if train_mu is None:
            # check if mu was initialized with MLE
            train_mu = self._train_mu
        if train_r is None:
            # check if r was initialized with MLE
            train_r = self._train_r

        if train_mu:
            if train_r:
                train_op = self.model.trainers.train_op_by_name(optim_algo)
            else:
                train_op = self.model.trainers_a_only.train_op_by_name(optim_algo)
        else:
            if train_r:
                train_op = self.model.trainers_b_only.train_op_by_name(optim_algo)
            else:
                logger.info("No training necessary; returning")
                return

        super().train(*args,
                      feed_dict={"learning_rate:0": learning_rate},
                      convergence_criteria=convergence_criteria,
                      loss_window_size=loss_window_size,
                      stop_at_loss_change=stop_at_loss_change,
                      train_op=train_op,
                      **kwargs)

    @property
    def input_data(self):
        return self._input_data

    def train_sequence(self, training_strategy=TrainingStrategy.AUTO):
        if isinstance(training_strategy, Enum):
            training_strategy = training_strategy.value
        elif isinstance(training_strategy, str):
            training_strategy = self.TrainingStrategy[training_strategy].value

        if training_strategy is None:
            if not self._train_mu:
                training_strategy = self.TrainingStrategy.PRE_INITIALIZED.value
            else:
                training_strategy = self.TrainingStrategy.DEFAULT.value

        for idx, d in enumerate(training_strategy):
            logger.info("Beginning with training sequence #%d", idx + 1)
            self.train(**d)
            logger.info("Training sequence #%d complete", idx + 1)

    # @property
    # def mu(self):
    #     return self.to_xarray("mu")
    #
    # @property
    # def r(self):
    #     return self.to_xarray("r")
    #
    # @property
    # def sigma2(self):
    #     return self.to_xarray("sigma2")

    @property
    def a(self):
        return self.to_xarray("a", coords=self.input_data.data.coords)

    @property
    def b(self):
        return self.to_xarray("b", coords=self.input_data.data.coords)

    @property
    def batch_loss(self):
        return self.to_xarray("loss")

    @property
    def batch_gradient(self):
        return self.to_xarray("gradient", coords=self.input_data.data.coords)

    @property
    def loss(self):
        return self.to_xarray("full_loss")

    @property
    def gradient(self):
        return self.to_xarray("full_gradient", coords=self.input_data.data.coords)

    @property
    def hessian_diagonal(self):
        return self.to_xarray("hessian_diagonal", coords=self.input_data.data.coords)

    def finalize(self):
        store = XArrayEstimatorStore(self)
        self.close_session()
        return store