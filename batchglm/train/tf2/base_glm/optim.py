from .external import pkg_constants
import tensorflow as tf
from .external import OptimizerBase
import abc
import numpy as np


class SecondOrderOptim(OptimizerBase, metaclass=abc.ABCMeta):

    """
    Superclass for NR and IRLS
    """

    def _norm_log_likelihood(self, log_probs):
        return tf.reduce_mean(log_probs, axis=0, name="log_likelihood")

    def _norm_neg_log_likelihood(self, log_probs):
        return - self._norm_log_likelihood(log_probs)

    def _resource_apply_dense(self, grad, handle, apply_state=None):

        update_op = handle.assign_add(grad, read_value=False)

        return update_op

    def _resource_apply_sparse(self, grad, handle, apply_state=None):

        raise NotImplementedError('Applying SparseTensor currently not possible.')

    def get_config(self):

        config = {"name": "SOO"}
        return config

    def _create_slots(self, var_list):

        self.add_slot(var_list[0], 'mu_r')

    def _trust_region_ops(
            self,
            x_batch,
            likelihood,
            proposed_vector,
            proposed_gain,
            compute_a,
            compute_b,
            batch_features,
            ll_prev
    ):
        # Load hyper-parameters:
        assert pkg_constants.TRUST_REGION_ETA0 < pkg_constants.TRUST_REGION_ETA1, \
            "eta0 must be smaller than eta1"
        assert pkg_constants.TRUST_REGION_ETA1 <= pkg_constants.TRUST_REGION_ETA2, \
            "eta1 must be smaller than or equal to eta2"
        assert pkg_constants.TRUST_REGION_T1 <= 1, "t1 must be smaller than 1"
        assert pkg_constants.TRUST_REGION_T2 >= 1, "t1 must be larger than 1"
        # Set trust region hyper-parameters
        eta0 = tf.constant(pkg_constants.TRUST_REGION_ETA0, dtype=self._dtype)
        eta1 = tf.constant(pkg_constants.TRUST_REGION_ETA1, dtype=self._dtype)
        eta2 = tf.constant(pkg_constants.TRUST_REGION_ETA2, dtype=self._dtype)
        if self.gd and compute_b:
            t1 = tf.constant(pkg_constants.TRUST_REGIONT_T1_IRLS_GD_TR_SCALE, dtype=self._dtype)
        else:
            t1 = tf.constant(pkg_constants.TRUST_REGION_T1, dtype=self._dtype)
        t2 = tf.constant(pkg_constants.TRUST_REGION_T2, dtype=self._dtype)
        upper_bound = tf.constant(pkg_constants.TRUST_REGION_UPPER_BOUND, dtype=self._dtype)

        # Phase I: Perform a trial update.
        # Propose parameter update:

        self.model.params_copy.assign_sub(proposed_vector)
        # Phase II: Evaluate success of trial update and complete update cycle.
        # Include parameter updates only if update improves cost function:
        new_likelihood = self.model.calc_ll([*x_batch], keep_previous_params_copy=True)[0]
        delta_f_actual = self._norm_neg_log_likelihood(likelihood) - self._norm_neg_log_likelihood(new_likelihood)

        if batch_features:

            indices = tf.where(tf.logical_not(self.model.model_vars.converged))
            updated_lls = tf.scatter_nd(indices, delta_f_actual, shape=ll_prev.shape)
            delta_f_actual = np.where(self.model.model_vars.converged, ll_prev, updated_lls.numpy())
            update_var = tf.transpose(tf.scatter_nd(
                indices,
                tf.transpose(proposed_vector),
                shape=(self.model.model_vars.n_features, proposed_vector.get_shape()[0])
            ))

            gain_var = tf.transpose(tf.scatter_nd(
                indices,
                proposed_gain,
                shape=([self.model.model_vars.n_features])))
        else:
            update_var = proposed_vector
            gain_var = proposed_gain
        delta_f_ratio = tf.divide(delta_f_actual, gain_var)

        # Compute parameter updates.g
        update_theta = tf.logical_and(delta_f_actual > eta0, tf.logical_not(self.model.model_vars.converged))
        update_theta_numeric = tf.expand_dims(tf.cast(update_theta, self._dtype), axis=0)
        keep_theta_numeric = tf.ones_like(update_theta_numeric) - update_theta_numeric
        if batch_features:
            params = tf.transpose(tf.scatter_nd(
                indices,
                tf.transpose(self.model.params_copy),
                shape=(self.model.model_vars.n_features, self.model.params.get_shape()[0])
            ))

            theta_new_tr = tf.add(
                tf.multiply(self.model.params, keep_theta_numeric),
                tf.multiply(params, update_theta_numeric)
            )


            #self.model.params.assign_(tf.multiply(params, update_theta_numeric))

        else:
            params = self.model.params_copy
            theta_new_tr = tf.add(
                tf.multiply(params + update_var, keep_theta_numeric),  # old values
                tf.multiply(params, update_theta_numeric)  # new values
            )
        self.model.params.assign(theta_new_tr)
        self.model.model_vars.updated = update_theta.numpy()

        # Update trusted region accordingly:
        decrease_radius = tf.logical_or(
            delta_f_actual <= eta0,
            tf.logical_and(delta_f_ratio <= eta1, tf.logical_not(self.model.model_vars.converged))
        )
        increase_radius = tf.logical_and(
            delta_f_actual > eta0,
            tf.logical_and(delta_f_ratio > eta2, tf.logical_not(self.model.model_vars.converged))
        )
        keep_radius = tf.logical_and(tf.logical_not(decrease_radius),
                                     tf.logical_not(increase_radius))
        radius_update = tf.add_n([
            tf.multiply(t1, tf.cast(decrease_radius, self._dtype)),
            tf.multiply(t2, tf.cast(increase_radius, self._dtype)),
            tf.multiply(tf.ones_like(t1), tf.cast(keep_radius, self._dtype))
        ])

        if self.gd and compute_b and not compute_a:
            tr_radius = self.tr_radius_b
        else:
            tr_radius = self.tr_radius

        radius_new = tf.minimum(tf.multiply(tr_radius, radius_update), upper_bound)
        tr_radius.assign(radius_new)

    def __init__(self, dtype: tf.dtypes.DType, trusted_region_mode: bool, model: tf.keras.Model, name: str):

        self.model = model
        self.gd = name in ['IRLS_GD', 'IRLS_GD_TR']

        super(SecondOrderOptim, self).__init__(name)

        self._dtype = dtype
        self.trusted_region_mode = trusted_region_mode
        if trusted_region_mode:

            self.tr_radius = tf.Variable(
                np.zeros(shape=[self.model.model_vars.n_features]) + pkg_constants.TRUST_REGION_RADIUS_INIT,
                dtype=self._dtype, trainable=False
            )
            if self.gd:
                self.tr_radius_b = tf.Variable(
                    np.zeros(shape=[self.model.model_vars.n_features]) + pkg_constants.TRUST_REGION_RADIUS_INIT,
                    dtype=self._dtype, trainable=False
                )

            self.tr_ll_prev = tf.Variable(np.zeros(shape=[self.model.model_vars.n_features]), trainable=False)
            self.tr_pred_gain = tf.Variable(np.zeros(shape=[self.model.model_vars.n_features]), trainable=False)

        else:

            self.tr_radius = tf.Variable(np.array([np.inf]), dtype=self._dtype, trainable=False)

    @abc.abstractmethod
    def perform_parameter_update(self, inputs):
        pass

    def _newton_type_update(self, lhs, rhs, psd):

        new_rhs = tf.expand_dims(rhs, axis=-1)
        res = tf.linalg.lstsq(lhs, new_rhs, fast=False)
        delta_t = tf.squeeze(res, axis=-1)
        update_tensor = tf.transpose(delta_t)
        return update_tensor

    def _pad_updates(
            self,
            update_raw,
            compute_a,
            compute_b
    ):
        # Pad update vectors to receive update tensors that match
        # the shape of model_vars.params.
        if compute_a:
            if compute_b:
                netwon_type_update = update_raw
            else:
                netwon_type_update = tf.concat([
                    update_raw,
                    tf.zeros(shape=(self.model.model_vars.b_var.get_shape()[0], update_raw.get_shape()[1]),
                             dtype=self._dtype)
                ], axis=0)

        elif compute_b:
            netwon_type_update = tf.concat([
                tf.zeros(shape=(self.model.model_vars.a_var.get_shape()[0], update_raw.get_shape()[1]),
                         dtype=self._dtype),
                update_raw
            ], axis=0)

        else:
            raise ValueError("No training necessary")

        return netwon_type_update

    def _trust_region_update(
            self,
            update_raw,
            radius_container,
            n_obs=None
    ):
        update_magnitude_sq = tf.reduce_sum(tf.square(update_raw), axis=0)
        update_magnitude = tf.where(
            condition=update_magnitude_sq > 0,
            x=tf.sqrt(update_magnitude_sq),
            y=tf.zeros_like(update_magnitude_sq)
        )
        update_magnitude_inv = tf.where(
            condition=update_magnitude > 0,
            x=tf.divide(
                tf.ones_like(update_magnitude),
                update_magnitude
            ),
            y=tf.zeros_like(update_magnitude)
        )
        update_norm = tf.multiply(update_raw, update_magnitude_inv)
        # the following switch is for irls_gd_tr (linear instead of newton)
        if n_obs is not None:
            update_magnitude /= n_obs
        update_scale = tf.minimum(
            radius_container,
            update_magnitude
        )
        proposed_vector = tf.multiply(
            update_norm,
            update_scale
        )

        return proposed_vector

    def _trust_region_newton_cost_gain(
            self,
            proposed_vector,
            neg_jac,
            hessian_fim,
            n_obs
    ):
        pred_cost_gain = tf.add(
            tf.einsum(
                'ni,in->n',
                neg_jac,
                proposed_vector
            ) / n_obs,
            0.5 * tf.einsum(
                'nix,xin->n',
                tf.einsum('inx,nij->njx',
                          tf.expand_dims(proposed_vector, axis=-1),
                          hessian_fim),
                tf.expand_dims(proposed_vector, axis=0)
            ) / tf.square(n_obs)
        )
        return pred_cost_gain


class NR(SecondOrderOptim):

    def _get_updates(self, lhs, rhs, psd, compute_a, compute_b):

        update_raw = self._newton_type_update(lhs=lhs, rhs=rhs, psd=psd)
        update = self._pad_updates(update_raw, compute_a, compute_b)

        return update_raw, update

    def perform_parameter_update(self, inputs, compute_a=True, compute_b=True, batch_features=False, prev_ll=None):

        x_batch, log_probs, jacobians, hessians, psd, n_obs = inputs
        if not (compute_a or compute_b):
            raise ValueError(
                "Nothing can be trained. Please make sure at least one of train_mu and train_r is set to True.")

        update_raw, update = self._get_updates(hessians, jacobians, psd, compute_a, compute_b)

        if self.trusted_region_mode:

            n_obs = tf.cast(n_obs, dtype=self._dtype)
            if batch_features:
                radius_container = tf.boolean_mask(
                    tensor=self.tr_radius,
                    mask=tf.logical_not(self.model.model_vars.converged))
            else:
                radius_container = self.tr_radius
            tr_proposed_vector = self._trust_region_update(
                update_raw=update_raw,
                radius_container=radius_container
            )
            tr_pred_cost_gain = self._trust_region_newton_cost_gain(
                proposed_vector=tr_proposed_vector,
                neg_jac=jacobians,
                hessian_fim=hessians,
                n_obs=n_obs
            )

            tr_proposed_vector_pad = self._pad_updates(
                update_raw=tr_proposed_vector,
                compute_a=compute_a,
                compute_b=compute_b
            )

            self._trust_region_ops(
                x_batch=x_batch,
                likelihood=log_probs,
                proposed_vector=tr_proposed_vector_pad,
                proposed_gain=tr_pred_cost_gain,
                compute_a=compute_a,
                compute_b=compute_b,
                batch_features=batch_features,
                ll_prev=prev_ll
            )

        else:
            if batch_features:
                indices = tf.where(tf.logical_not(self.model.model_vars.converged))
                update_var = tf.transpose(
                    tf.scatter_nd(
                        indices,
                        tf.transpose(update),
                        shape=(self.model.model_vars.n_features, update.get_shape()[0])
                    )
                )
            else:
                update_var = update
            self.model.params.assign_sub(update_var)


class IRLS(SecondOrderOptim):

    def _calc_proposed_vector_and_pred_cost_gain(
            self,
            update_x,
            radius_container,
            n_obs,
            gd,
            neg_jac_x,
            fim_x=None
    ):
        """
        Calculates the proposed vector and predicted cost gain for either mean or scale part.
        :param update_x: tf.tensor coefficients x features ? TODO

        :param radius_container: tf.tensor ? x ? TODO

        :param n_obs: ? TODO
            Number of observations in current batch.
        :param gd: boolean
            If True, the proposed vector and predicted cost gain are
            calculated by linear functions related to IRLS_GD(_TR) optimizer.
            If False, use newton functions for IRLS_TR optimizer instead.
        :param neg_jac_x: tf.Tensor coefficients x features ? TODO
            Upper (mu part) or lower (r part) of negative jacobian matrix
        :param fim_x
            Upper (mu part) or lower (r part) of Fisher Inverse Matrix.
            Defaults to None, is only needed if gd is False
        :return proposed_vector_x, pred_cost_gain_x
            Returns proposed vector and predicted cost gain after
            trusted region update for either mu or r part, depending on x
        """

        proposed_vector_x = self._trust_region_update(
            update_raw=update_x,
            radius_container=radius_container,
            n_obs=n_obs if gd else None
        )
        # here, functions have different number of arguments, thus
        # must be written out
        if gd:
            pred_cost_gain_x = self._trust_region_linear_cost_gain(
                proposed_vector=proposed_vector_x,
                neg_jac=neg_jac_x
            )
        else:
            pred_cost_gain_x = self._trust_region_newton_cost_gain(
                proposed_vector=proposed_vector_x,
                neg_jac=neg_jac_x,
                hessian_fim=fim_x,
                n_obs=n_obs
            )

        return proposed_vector_x, pred_cost_gain_x

    def _trust_region_linear_cost_gain(
            self,
            proposed_vector,
            neg_jac
    ):
        pred_cost_gain = tf.reduce_sum(tf.multiply(
            proposed_vector,
            tf.transpose(neg_jac)
        ), axis=0)
        return pred_cost_gain

    def perform_parameter_update(self, inputs, compute_a=True, compute_b=True, batch_features=False, prev_ll=None):

        x_batch, log_probs, jac_a, jac_b, fim_a, fim_b, psd, n_obs = inputs
        if not (compute_a or compute_b):
            raise ValueError(
                "Nothing can be trained. Please make sure at least one of train_mu and train_r is set to True.")
        # Compute a and b model updates separately.
        if compute_a:
            # The FIM of the mean model is guaranteed to be
            # positive semi-definite and can therefore be inverted
            # with the Cholesky decomposition. This information is
            # passed here with psd=True.
            update_a = self._newton_type_update(
                lhs=fim_a,
                rhs=jac_a,
                psd=True
            )
        if compute_b:

            if self.gd:
                update_b = tf.transpose(jac_b)

            else:
                update_b = self._newton_type_update(
                    lhs=fim_b,
                    rhs=jac_b,
                    psd=False
                )

        if not self.trusted_region_mode:
            if compute_a:
                if compute_b:
                    update_raw = tf.concat([update_a, update_b], axis=0)
                else:
                    update_raw = update_a
            else:
                update_raw = update_b

            update = self._pad_updates(
                update_raw=update_raw,
                compute_a=compute_a,
                compute_b=compute_b
            )

            if batch_features:
                indices = tf.where(tf.logical_not(self.model.model_vars.converged))
                update_var = tf.transpose(
                    tf.scatter_nd(
                        indices,
                        tf.transpose(update),
                        shape=(self.model.model_vars.n_features, update.get_shape()[0])
                    )
                )
            else:
                update_var = update
            self.model.params.assign_sub(update_var)

        else:

            n_obs = tf.cast(n_obs, dtype=self._dtype)
            # put together update_raw based on proposed vector and cost gain depending on train_r and train_mu
            if compute_b:
                if compute_a:
                    if batch_features:
                        radius_container = tf.boolean_mask(
                            tensor=self.tr_radius,
                            mask=tf.logical_not(self.model.model_vars.converged))
                    else:
                        radius_container = self.tr_radius
                    tr_proposed_vector_b, tr_pred_cost_gain_b = self._calc_proposed_vector_and_pred_cost_gain(
                        update_b, radius_container, n_obs, self.gd, jac_b, fim_b)

                    tr_proposed_vector_a, tr_pred_cost_gain_a = self._calc_proposed_vector_and_pred_cost_gain(
                        update_a, radius_container, n_obs, False, jac_a, fim_a)

                    tr_update_raw = tf.concat([tr_proposed_vector_a, tr_proposed_vector_b], axis=0)
                    tr_pred_cost_gain = tf.add(tr_pred_cost_gain_a, tr_pred_cost_gain_b)

                else:
                    radius_container = self.tr_radius_b if self.gd else self.tr_radius
                    if batch_features:
                        radius_container = tf.boolean_mask(
                            tensor=radius_container,
                            mask=tf.logical_not(self.model.model_vars.converged))

                    tr_proposed_vector_b, tr_pred_cost_gain_b = self._calc_proposed_vector_and_pred_cost_gain(
                        update_b, radius_container, n_obs, self.gd, jac_b, fim_b)

                    # directly apply output of calc_proposed_vector_and_pred_cost_gain to tr_update_raw
                    # and tr_pred_cost_gain
                    tr_update_raw = tr_proposed_vector_b
                    tr_pred_cost_gain = tr_pred_cost_gain_b
            else:
                if batch_features:
                    radius_container = tf.boolean_mask(
                        tensor=self.tr_radius,
                        mask=tf.logical_not(self.model.model_vars.converged))
                else:
                    radius_container = self.tr_radius
                # here train_r is False AND train_mu is true, so the output of the function can directly be applied to
                # tr_update_raw and tr_pred_cost_gain, similar to train_r = True and train_mu = False
                tr_update_raw, tr_pred_cost_gain = self._calc_proposed_vector_and_pred_cost_gain(
                    update_a, radius_container, n_obs, False, jac_a, fim_a)

            # perform update
            tr_update = self._pad_updates(
                update_raw=tr_update_raw,
                compute_a=compute_a,
                compute_b=compute_b
            )

            self._trust_region_ops(
                x_batch,
                log_probs,
                tr_update,
                tr_pred_cost_gain,
                compute_a,
                compute_b,
                batch_features,
                prev_ll
            )