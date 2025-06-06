# This file is part of sbi, a toolkit for simulation-based inference. sbi is licensed
# under the Apache License Version 2.0, see <https://www.apache.org/licenses/>

from copy import deepcopy
from math import floor
from typing import Any, Callable, Optional, Tuple, Union

import torch
import torch.nn.functional as F
from pyknos.nflows.nn import nets
from torch import Tensor, nn, relu
from torch.distributions import Distribution
from torch.nn.utils.clip_grad import clip_grad_norm_
from torch.optim.adam import Adam
from torch.utils import data
from torch.utils.data.sampler import SubsetRandomSampler, WeightedRandomSampler

from sbi.samplers import rejection
from sbi.samplers.importance.sir import sampling_importance_resampling
from sbi.sbi_types import Shape
from sbi.utils.sbiutils import (
    get_simulations_since_round,
    handle_invalid_x,
    standardizing_net,
    z_score_parser,
)
from sbi.utils.torchutils import ensure_theta_batched
from sbi.utils.user_input_checks import validate_theta_and_x


def build_input_layer(
    batch_theta: Tensor,
    z_score_theta: Optional[str] = "independent",
    embedding_net_theta: nn.Module = nn.Identity(),
) -> nn.Module:
    r"""Builds input layer for the `RestrictionEstimator` with option to z-score.

    The classifier used in the `RestrictionEstimator` will receive batches of $\theta$s.

    Args:
        batch_theta: Batch of $\theta$s, used to infer dimensionality and (optional)
            z-scoring.
        z_score_theta: Whether to z-score parameters $\theta$ before passing them into
            the network, can take one of the following:
            - `none`, or None: do not z-score.
            - `independent`: z-score each dimension independently.
            - `structured`: treat dimensions as related, therefore compute mean and std
            over the entire batch, instead of per-dimension. Should be used when each
            sample is, for example, a time series or an image.
        embedding_net_theta: Optional embedding network for $\theta$s.

    Returns:
        Input layer with optional embedding net and z-scoring.
    """
    z_score_theta_bool, structured_theta = z_score_parser(z_score_theta)
    if z_score_theta_bool:
        input_layer = nn.Sequential(
            standardizing_net(batch_theta, structured_theta), embedding_net_theta
        )
    else:
        input_layer = embedding_net_theta

    return input_layer


class RestrictionEstimator:
    """Classifier to estimate regions of the prior that give good simulation results."""

    def __init__(
        self,
        prior: Distribution,
        model: Union[str, Callable] = "resnet",
        decision_criterion: Union[str, Callable] = "nan",
        hidden_features: int = 100,
        num_blocks: int = 2,
        dropout_probability: float = 0.5,
        z_score: Optional[str] = "independent",
        embedding_net: nn.Module = nn.Identity(),
    ) -> None:
        r"""
        Estimator that trains a classifier to restrict the prior.

        The classifier learns to distinguish `valid` simulation outputs from `invalid`
        simulation outputs.

        Reference:
        Deistler et al. (2022): "Energy-efficient network activity from disparate
        circuit parameters"

        Args:
            prior: Prior distribution.
            model: Neural network used to distinguish valid from invalid samples. If it
                is a string, use a pre-configured network of the provided type (either
                mlp or resnet). Alternatively, a function that builds a custom
                neural network can be provided. The function will be called with the
                first batch of parameters (theta,), which can thus be used for shape
                inference and potentially for z-scoring. It needs to return a PyTorch
                `nn.Module` implementing the classifier.
            decision_criterion: Callable that takes in the simulation output $x$ and
                outputs whether $x$ is counted as `valid` simulation (output 1) or as a
                `invalid` simulation (output 0). By default, the function checks
                whether a simulation output $x$ contains at least one `nan` or `inf`.
            hidden_features: Number of hidden units of the classifier if `model` is a
                string.
            num_blocks: Number of hidden layers of the classifier if `model` is a
                string.
            dropout_probability: Dropout probability of the classifier if `model` is
                `resnet`.
            z_score: Whether to z-score the parameters $\theta$ used to train the
                classifier.
            embedding_net: Neural network used to encode the parameters before they are
                passed to the classifier.
        """
        self._prior = prior
        self._classifier = None

        self._device = "cpu"  # TODO hot fix to prevent the tests from crashing

        if isinstance(model, str):
            self._model = model
            self._hidden_features = hidden_features
            self._num_blocks = num_blocks
            self._dropout_probability = dropout_probability
            self._z_score = z_score
            self._embedding_net = embedding_net

            if model == "resnet":
                self._build_nn = self.build_resnet
            elif model == "mlp":
                self._build_nn = self.build_mlp
            else:
                raise NameError(
                    f"The `model` must be either of [resnet|mlp]. You passed {model}."
                )
        else:
            self._build_nn = model

        self._valid_or_invalid_criterion = decision_criterion

        self._theta_roundwise = []
        self._x_roundwise = []
        self._label_roundwise = []
        self._data_round_index = []
        self._validation_log_probs = []

    def build_resnet(self, theta) -> nn.Module:
        classifier = nets.ResidualNet(
            in_features=theta.shape[1],
            out_features=2,
            hidden_features=self._hidden_features,
            context_features=None,
            num_blocks=self._num_blocks,
            activation=relu,
            dropout_probability=self._dropout_probability,
            use_batch_norm=True,
        )
        z_score_theta = self._z_score
        embedding_net_theta = self._embedding_net
        input_layer = build_input_layer(theta, z_score_theta, embedding_net_theta)
        classifier = nn.Sequential(input_layer, classifier)
        return classifier

    def build_mlp(self, theta) -> nn.Module:
        classifier = nn.Sequential(
            nn.Linear(theta.shape[1], self._hidden_features),
            nn.BatchNorm1d(self._hidden_features),
            nn.ReLU(),
            nn.Linear(self._hidden_features, self._hidden_features),
            nn.BatchNorm1d(self._hidden_features),
            nn.ReLU(),
            nn.Linear(self._hidden_features, 2),
        )
        z_score_theta = self._z_score
        embedding_net_theta = self._embedding_net
        input_layer = build_input_layer(theta, z_score_theta, embedding_net_theta)
        classifier = nn.Sequential(input_layer, classifier)
        return classifier

    def append_simulations(self, theta: Tensor, x: Tensor) -> "RestrictionEstimator":
        r"""
        Store parameters and simulation outputs to use them for training later.
        Data ar stored as entries in lists for each type of variable (parameter/data).

        Args:
            theta: Parameter sets.
            x: Simulation outputs.

        Returns:
            `RestrictionEstimator` object (returned so that this function is chainable).
        """

        theta, x = validate_theta_and_x(theta, x, training_device=self._device)

        if self._valid_or_invalid_criterion == "nan":
            label, _, _ = handle_invalid_x(x)
        else:
            assert isinstance(self._valid_or_invalid_criterion, Callable)
            label = self._valid_or_invalid_criterion(x)

        label = label.long()

        if self._data_round_index:
            self._data_round_index.append(self._data_round_index[-1] + 1)
        else:
            self._data_round_index.append(0)

        self._theta_roundwise.append(theta)
        self._x_roundwise.append(x)
        self._label_roundwise.append(label)

        return self

    def get_simulations(self, starting_round: int = 0) -> Tuple[Tensor, Tensor, Tensor]:
        r"""
        Return all $(\theta, x, label)$ pairs that have been passed to this object.

        The label had been inferred from the `valid_or_invalid_criterion`.
        """
        theta = get_simulations_since_round(
            self._theta_roundwise, self._data_round_index, starting_round
        )
        x = get_simulations_since_round(
            self._x_roundwise, self._data_round_index, starting_round
        )
        label = get_simulations_since_round(
            self._label_roundwise, self._data_round_index, starting_round
        )

        return theta, x, label

    def train(
        self,
        training_batch_size: int = 200,
        learning_rate: float = 5e-4,
        validation_fraction: float = 0.1,
        stop_after_epochs: int = 20,
        max_num_epochs: int = 2**31 - 1,
        clip_max_norm: Optional[float] = 5.0,
        loss_importance_weights: Union[bool, float] = False,
        subsample_invalid_sims: Union[float, str] = 1.0,
    ) -> torch.nn.Module:
        r"""
        Train the classifier to distinguish parameters with `valid`|`invalid` outputs.

        Args:
            training_batch_size: Training batch size.
            learning_rate: Learning rate for Adam optimizer.
            validation_fraction: The fraction of data to use for validation.
            stop_after_epochs: The number of epochs to wait for improvement on the
                validation set before terminating training.
            max_num_epochs: Maximum number of epochs to run. If reached, we stop
                training even when the validation loss is still decreasing. If None, we
                train until validation loss increases (see also `stop_after_epochs`).
            clip_max_norm: Value at which to clip the total gradient norm in order to
                prevent exploding gradients. Use None for no clipping.
            loss_importance_weights: If `bool`: whether or not to reweigh the loss such
                that the prior between `valid` and `invalid` simulations is uniform.
                This is one way to deal with imbalanced data (e.g. 99% invalid
                simulations). If you want to reweigh with a custom weight, pass a
                `float`. The value assigned will be the reweighing factor for invalid
                simulations, (1-reweigh_factor) will be the factor for good simulations.
            subsample_invalid_sims: Sampling weight of invalid simulations. This can be
                useful when the fraction of invalid simulations is extremely high and
                one wants to train on a larger fraction of valid simulations. This
                factor has to be in [0, 1]. If it is `auto`, automatically infer
                subsample weights such that the data is balanced.
        """

        theta: Tensor = torch.cat(self._theta_roundwise)
        label: Tensor = torch.cat(self._label_roundwise)

        # Get indices for permutation of the data.
        num_examples = len(theta)
        permuted_indices = torch.randperm(num_examples)
        num_training_examples = int((1 - validation_fraction) * num_examples)
        num_validation_examples = num_examples - num_training_examples
        train_indices, val_indices = (
            permuted_indices[:num_training_examples],
            permuted_indices[num_training_examples:],
        )

        # The ratio of `valid` and `invalid` simulation outputs might not be balanced.
        # E.g. if there are fewer `valid` datapoints, one might want to sample them
        # more often (i.e. show them to the neural network more often). Such a sampler
        # is implemented below. Also see: https://discuss.pytorch.org/t/29907
        subsample_weights: Tensor = torch.ones(num_examples)
        if subsample_invalid_sims == "auto":
            subsample_invalid = float(label.sum()) / float(theta.shape[0] - label.sum())
        else:
            assert isinstance(subsample_invalid_sims, float)
            subsample_invalid = subsample_invalid_sims

        subsample_weights[torch.logical_not(label.bool())] = subsample_invalid

        subsample_weights = deepcopy(subsample_weights)
        subsample_weights[val_indices] = 0.0

        # Dataset is shared for training and validation loaders.
        dataset = data.TensorDataset(theta, label)

        # Create neural_net and validation loaders using a subset sampler.
        train_loader = data.DataLoader(
            dataset,
            batch_size=training_batch_size,
            drop_last=True,
            sampler=WeightedRandomSampler(
                subsample_weights.tolist(),
                int(subsample_weights.sum()),
                replacement=False,
            ),
        )
        val_loader = data.DataLoader(
            dataset,
            batch_size=min(
                max(200, training_batch_size), num_examples - num_training_examples
            ),
            shuffle=False,
            drop_last=True,
            sampler=SubsetRandomSampler(val_indices.tolist()),
        )

        if self._classifier is None:
            self._classifier = self._build_nn(theta[train_indices])

        # If we are in the first round, save the validation data in order to be able to
        # tune the classifier threshold.
        if max(self._data_round_index) == 0:
            self._first_round_validation_theta = theta[val_indices]
            self._first_round_validation_label = label[val_indices]

        optimizer = Adam(
            list(self._classifier.parameters()),
            lr=learning_rate,
        )

        # Compute the fraction of good simulations in dataset.
        if loss_importance_weights:
            if isinstance(loss_importance_weights, bool):
                good_sim_fraction = torch.sum(label, dtype=torch.float) / label.shape[0]
                importance_weights = good_sim_fraction
            else:
                importance_weights = loss_importance_weights
        else:
            importance_weights = 0.5

        # Factor of two such that the average learning rate remains the same.
        # Needed because the average of reweigh_factor and 1-reweigh_factor will be 0.5
        # only.
        importance_weights = 2 * torch.tensor([
            importance_weights,
            1 - importance_weights,
        ])

        criterion = nn.CrossEntropyLoss(importance_weights, reduction="none")

        epoch, self._val_log_prob = 0, float("-Inf")
        while epoch <= max_num_epochs and not self._converged(epoch, stop_after_epochs):
            self._classifier.train()
            for parameters, observations in train_loader:
                optimizer.zero_grad()
                outputs = self._classifier(parameters)
                loss = criterion(outputs, observations).mean()
                loss.backward()
                if clip_max_norm is not None:
                    clip_grad_norm_(
                        self._classifier.parameters(),
                        max_norm=clip_max_norm,
                    )
                optimizer.step()

            epoch += 1

            # calculate validation performance
            self._classifier.eval()

            val_loss = 0.0
            with torch.no_grad():
                for parameters, observations in val_loader:
                    outputs = self._classifier(parameters)
                    loss = criterion(outputs, observations)
                    loss[~observations.bool()] *= subsample_invalid_sims
                    val_loss += loss.sum().item()
            self._val_log_prob = -val_loss / num_validation_examples
            self._validation_log_probs.append(self._val_log_prob)

            print("Training neural network. Epochs trained: ", epoch, end="\r")

        return deepcopy(self._classifier)

    def restrict_prior(
        self,
        classifier: Optional[nn.Module] = None,
        allowed_false_negatives: float = 0.0,
        reweigh_factor: Optional[float] = None,
    ) -> "RestrictedPrior":
        r"""
        Return the restricted prior.

        The restricted prior (Deistler et al. 2020, in preparation) is the part of the
        prior that can produce `valid` simulations. More formally, the restricted prior
        $p_r(\theta)$ is:

        $p_r(\theta) = c \cdot p(\theta) if \theta \in support(p(\theta|x=`valid`))$
        $p_r(\theta) = 0 otherwise$.

        We sample from the restricted prior by sampling from the prior and then
        rejecting if the classifier predicts that the simulation output can not be
        `valid`.

        Args:
            classifier: Classifier that is used to predict whether parameter sets are
                `valid` or `invalid`.
            allowed_false_negatives: Fraction of false-negative predictions the
                classifier is allowed to make. The threshold of the classifier will be
                tuned such that this criterion is fulfilled. A high value will lead to
                the classifier rejecting more parameter sets, which will give many
                `valid` parameter sets. However, a high value also means that some
                potentially `valid` parameter sets will be missed. Inference is only
                **exact** for `allowed_false_negatives=0.0`. The value specified here
                corresponds approximately to the fraction of parameter sets that will
                be systematically missed by the inference procedure.
            reweigh_factor: Post-hoc correction factor. Should be in [0, 1]. A large
                reweigh factor will increase the probability of predicting a `invalid`
                simulation.

        Returns:
            Restricted prior with `.sample()` and `.predict()` methods.

        """
        if classifier is None:
            assert self._classifier is not None, "Classifier must be trained first."
            classifier_ = self._classifier
        else:
            classifier_ = classifier

        classifier_.zero_grad(set_to_none=True)

        accept_reject_fn = AcceptRejectFunction(
            self._classifier,
            self._first_round_validation_theta,
            self._first_round_validation_label,
            allowed_false_negatives=allowed_false_negatives,
            reweigh_factor=reweigh_factor,
        )

        return RestrictedPrior(self._prior, accept_reject_fn)

    def _converged(self, epoch: int, stop_after_epochs: int) -> bool:
        r"""
        Return whether the training converged yet and save best model state so far.

        Checks for improvement in validation performance over previous epochs.

        Args:
            epoch: Current epoch in training.
            stop_after_epochs: How many fruitless epochs to let pass before stopping.

        Returns:
            Whether the training has stopped improving, i.e. has converged.
        """
        converged = False

        assert self._classifier is not None, "Classifier must be trained first."
        posterior_nn = self._classifier

        # (Re)-start the epoch count with the first epoch or any improvement.
        if epoch == 0 or self._val_log_prob > self._best_val_log_prob:
            self._best_val_log_prob = self._val_log_prob
            self._epochs_since_last_improvement = 0
            self._best_model_state_dict = deepcopy(posterior_nn.state_dict())
        else:
            self._epochs_since_last_improvement += 1

        # If no validation improvement over many epochs, stop training.
        if self._epochs_since_last_improvement > stop_after_epochs - 1:
            posterior_nn.load_state_dict(self._best_model_state_dict)
            converged = True

        return converged


def get_density_thresholder(
    dist: Any,
    quantile: float = 1e-4,
    num_samples_to_estimate_support: int = 1_000_000,
) -> Callable:
    """Returns function that thresholds a density at a particular `1-quantile`.

    Reference:
    Deistler et al. (2022): "Truncated proposals for scalable and hassle-free
    simulation-based inference"

    Args:
        dist: Probability distribution to be thresholded, must have `.sample()` and
            `.log_prob()`.
        quantile: The returned function will be `True` for $\theta$ within the
            `1-quantile` high-probability region of the distribution. In other words:
            `quantile` is the fraction of mass that is excluded from `dist`.
        num_samples_to_estimate_support: The number of samples that are drawn from
            `dist` in order to obtain the threshold. Higher values are more accurate
            but slower.

    Returns:
        Callabe which is true for $\theta$ in the `1-quantile` high-probability region
        of the `dist`.
    """
    samples = dist.sample((num_samples_to_estimate_support,))
    log_probs = dist.log_prob(samples)
    sorted_log_probs, _ = torch.sort(log_probs)
    log_prob_threshold = sorted_log_probs[
        int(quantile * num_samples_to_estimate_support)
    ]

    def density_thresholder(theta: Tensor) -> Tensor:
        theta_log_probs = dist.log_prob(theta)
        predictions = theta_log_probs > log_prob_threshold
        return predictions.bool()

    return density_thresholder


class AcceptRejectFunction:
    def __init__(
        self,
        classifier: Any,
        validation_theta: Tensor,
        validation_label: Tensor,
        allowed_false_negatives: Optional[float] = None,
        reweigh_factor: Optional[float] = None,
        print_fp_rate: bool = False,
        safety_margin: Optional[Union[str, float]] = "frequentist",
    ) -> None:
        self._classifier = classifier
        self._validation_theta = validation_theta
        self._validation_label = validation_label
        self._allowed_false_negatives = allowed_false_negatives
        self._reweigh_factor = reweigh_factor
        self._print_fp_rate = print_fp_rate
        self._safety_margin = safety_margin

        assert (
            allowed_false_negatives is None or reweigh_factor is None
        ), """Both the `allowed_false_negatives` and the `reweigh_factor` are set. You
            can only set one of them."""

        valid_val_theta = validation_theta[validation_label.bool()]
        num_valid = valid_val_theta.shape[0]
        clf_probs = F.softmax(classifier.forward(valid_val_theta), dim=1)[:, 1]

        if allowed_false_negatives == 0.0:
            if safety_margin is None:
                self._classifier_thr = torch.min(clf_probs)
            elif isinstance(safety_margin, float):
                self._classifier_thr = torch.min(clf_probs) - safety_margin
            elif safety_margin == "frequentist":
                # We seek the minimum classifier output, not the maximum, as it usually
                # is in the `German Tank Problem`. Hence, we transform the outputs with
                # (1-output), apply the estimator, and then transform back.
                tf_min_val = torch.max(1.0 - clf_probs)
                tf_estimate = tf_min_val + tf_min_val / num_valid
                self._classifier_thr = 1.0 - tf_estimate
            else:
                raise NameError(f"`safety_margin` {safety_margin} not supported.")
        else:
            assert allowed_false_negatives is not None, (
                "`allowed_false_negatives` must be set."
            )
            quantile_index = floor(num_valid * allowed_false_negatives)
            self._classifier_thr, _ = torch.kthvalue(clf_probs, quantile_index + 1)

        if self._print_fp_rate:
            self.print_false_positive_rate(
                self.__call__, self._validation_theta, self._validation_label
            )

    def __call__(self, theta):
        pred = F.softmax(self._classifier.forward(theta), dim=1)[:, 1]
        if self._reweigh_factor is None:
            threshold = self._classifier_thr
            predictions = pred > threshold
        else:
            probs_invalid = pred * self._reweigh_factor
            probs_valid = (1 - pred) * (1 - self._reweigh_factor)
            predictions = probs_valid > probs_invalid
        return predictions.bool()

    def print_false_positive_rate(
        self,
        accept_reject_fn: Callable,
        validation_theta: Tensor,
        validation_label: Tensor,
    ) -> float:
        r"""
        Print and return the rate of false positive predictions on the validation set.

        Returns:
            The false positive rate.
        """
        invalid_val_theta = validation_theta[~validation_label.bool()]
        predictions = accept_reject_fn(invalid_val_theta)
        num_false_positives = int(predictions.sum())
        fraction_false_positives = num_false_positives / invalid_val_theta.shape[0]
        print(
            f"Fraction of false positives: "
            f"{num_false_positives} / {invalid_val_theta.shape[0]} = "
            f"{fraction_false_positives:.3f}"
        )
        return fraction_false_positives


class RestrictedPrior(Distribution):
    """Distribution that restricts the prior distribution to a smaller region."""

    def __init__(
        self,
        prior: Distribution,
        accept_reject_fn: Callable,
        posterior: Optional[Any] = None,
        sample_with: str = "rejection",
        device: str = "cpu",
    ) -> None:
        r"""Initialize the simulation-informed prior.

        References:
        - Deistler et al. (2022): *Energy-efficient network activity from disparate
        circuit parameters*
        - Deistler et al. (2022): *Truncated proposals for scalable and hassle-free
        simulation-based inference*

        Args:
            prior: Prior distribution, will be used as proposal distribution whose
                samples will be evaluated by the classifier.
            accept_reject_fn: Callable that returns `True` inside the restricted region
                and `False` outside of it.
            posterior: Posterior distribution. Only used as proposal for `sir`.
            sample_with: Either of [`rejection`|`sir`]. Sets that method that is used
                to sample from the restricted prior. If `sir`, youu must have passed
                a `posterior` at initialization.
            device: Device used for sampling and evaluating.
        """
        super().__init__(validate_args=False)
        self._prior = prior
        self._accept_reject_fn = accept_reject_fn
        self._posterior = posterior  # Only used for SIR.
        self._sample_with = sample_with
        self._device = device
        self.acceptance_rate = None  # Only defined for rejection sampling.

    def sample(
        self,
        sample_shape: Shape = torch.Size(),
        sample_with: Optional[str] = None,
        max_sampling_batch_size: int = 10_000,
        oversampling_factor: int = 1024,
        save_acceptance_rate: bool = False,
        show_progress_bars: bool = False,
        print_rejected_frac: bool = True,
    ) -> Tensor:
        """
        Return samples from the `RestrictedPrior`.

        Samples are obtained by sampling from the prior, evaluating them under the
        trained classifier (`RestrictionEstimator`) and using only those that were
        accepted.

        Args:
            sample_shape: Shape of the returned samples.
            sample_with: Either of [`rejection`|`sir`]. Sets that method that is used
                to sample from the restricted prior. If `sir`, youu must have passed
                a `posterior` at initialization.
            max_sampling_batch_size: Batch size for drawing samples from the posterior.
                Takes effect only in the second iteration of the loop below, i.e., in
                case of leakage or `num_samples>max_sampling_batch_size`. Larger batch
                size speeds up sampling.
            oversampling_factor: Number of proposed samples for `sir` from which only
                one is selected based on its importance weight.
            save_acceptance_rate: If `True`, the acceptance rate is saved and such that
                it can potentially be used later in `log_prob()`.
            show_progress_bars: Whether to show a progressbar during sampling.
            print_rejected_frac: Whether to print the rejection rate of the restriction
                estimator during sampling.

        Returns:
            Samples from the `RestrictedPrior`.
        """
        num_samples = torch.Size(sample_shape).numel()
        sample_with = self._sample_with if sample_with is None else sample_with

        if sample_with == "rejection":
            samples, acceptance_rate = rejection.accept_reject_sample(
                proposal=self._prior.sample,
                accept_reject_fn=self._accept_reject_fn,
                num_samples=num_samples,
                show_progress_bars=show_progress_bars,
                max_sampling_batch_size=max_sampling_batch_size,
                alternative_method="sample_with='sir'",
            )
            # NOTE: This currently requires a float acceptance rate. A previous version
            # of accept_reject_sample returned a float. In favour to batched sampling
            # it now returns a tensor.
            acceptance_rate = acceptance_rate.min().item()

            if save_acceptance_rate:
                self.acceptance_rate = torch.as_tensor(acceptance_rate)
            if print_rejected_frac:
                print(
                    f"The `RestrictedPrior` rejected "
                    f"{(1.0 - acceptance_rate) * 100:.1f}% of prior samples. You will "
                    f"get a speed-up of {(1.0 / acceptance_rate - 1.0) * 100:.1f}%."
                )
        elif sample_with == "sir":
            assert self._posterior is not None, (
                "In order to use SIR sampling, you must provide a `posterior`: "
                "`RestrictionEstimator(..., posterior=posterior)`."
            )

            num_samples = torch.Size(sample_shape).numel()

            accept_reject_fn = lambda theta: self._accept_reject_fn(theta).type(
                torch.float32
            )

            samples = sampling_importance_resampling(
                accept_reject_fn,
                proposal=self._posterior,
                num_samples=num_samples,
                oversampling_factor=oversampling_factor,
                show_progress_bars=show_progress_bars,
                max_sampling_batch_size=max_sampling_batch_size,
                device=self._device,
            )
        else:
            raise ValueError("Only [rejection | sir] implemented as `method`")

        return samples.reshape((*sample_shape, -1)).to(self._device)

    def log_prob(
        self,
        theta: Tensor,
        norm_restricted_prior: bool = True,
        track_gradients: bool = False,
        prior_acceptance_params: Optional[dict] = None,
    ) -> Tensor:
        r"""Returns the log-probability of the restricted prior.

        Args:
            theta: Parameters $\theta$.
            norm_restricted_prior: Whether to enforce a normalized restricted prior
                density. The normalizing factor is calculated via rejection sampling,
                so if you need speedier but unnormalized log probability estimates set
                here `norm_restricted_prior=False`. The returned log probability is set
                to -∞ outside of the restriceted prior support regardless of this
                setting.
            track_gradients: Whether the returned tensor supports tracking gradients.
                This can be helpful for e.g. sensitivity analysis, but increases memory
                consumption.
            prior_acceptance_params: A `dict` of keyword arguments to override the
                default values of `prior_acceptance()`. Possible options are:
                `num_rejection_samples`, `force_update`, `show_progress_bars`, and
                `rejection_sampling_batch_size`. These parameters only have an effect
                if `norm_restricted_prior=True`.

        Returns:
            `(len(θ),)`-shaped log probability for θ in the support of the restricted
            prior, -∞ (corresponding to 0 probability) outside.
        """
        theta = ensure_theta_batched(torch.as_tensor(theta))

        with torch.set_grad_enabled(track_gradients):
            # Evaluate on device, move back to cpu for comparison with prior.
            prior_log_prob = self._prior.log_prob(theta)
            accepted = self._accept_reject_fn(theta).bool()

            masked_log_prob = torch.where(
                accepted,
                prior_log_prob,
                torch.tensor(float("-inf"), dtype=torch.float32),
            )

            if prior_acceptance_params is None:
                prior_acceptance_params = dict()  # use defaults
            log_factor = (
                torch.log(self.prior_acceptance(**prior_acceptance_params))
                if norm_restricted_prior
                else 0
            )

            return masked_log_prob - log_factor

    @torch.no_grad()
    def prior_acceptance(
        self,
        num_rejection_samples: int = 10_000,
        force_update: bool = False,
        show_progress_bars: bool = False,
        rejection_sampling_batch_size: int = 10_000,
    ) -> Tensor:
        """
        Return the fraction of prior samples accepted by the classifier.

        The factor is estimated from the acceptance probability during rejection
        sampling from the prior.

        Args:
            num_rejection_samples: Number of samples to estimate the acceptance rate.
            force_update: Whether to force update the acceptance rate.
            show_progress_bars: Whether to show progress bars during sampling.
            rejection_sampling_batch_size: Batch size for rejection sampling.

        Returns:
            Tensor of the estimated acceptance rate.
        """
        if self.acceptance_rate is None or force_update:
            self.sample(
                sample_shape=torch.Size((num_rejection_samples,)),
                sample_with="rejection",
                show_progress_bars=show_progress_bars,
                max_sampling_batch_size=rejection_sampling_batch_size,
                save_acceptance_rate=True,
            )
        # after calling sample, self.acceptance_rate will be a Tensor.
        return self.acceptance_rate  # type: ignore

    @property
    def mean(self) -> Tensor:
        """Mean of the restricted prior (not implemented)."""
        raise NotImplementedError("Mean is not implemented for RestrictedPrior.")

    @property
    def variance(self) -> Tensor:
        """Variance of the restricted prior (not implemented)."""
        raise NotImplementedError("Variance is not implemented for RestrictedPrior.")

    @property
    def support(self):
        # Return base prior's support or raise an error
        try:
            return self._prior.support
        except AttributeError as e:
            raise NotImplementedError(
                "Support is not implemented for this RestrictedPrior."
            ) from e
