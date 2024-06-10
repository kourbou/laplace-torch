from collections.abc import MutableMapping
from copy import deepcopy
from typing import Union, Any


import torch
from torch import nn

from torch.nn.utils import parameters_to_vector, vector_to_parameters

from laplace.baselaplace import (
    DiagLaplace,
    FullLaplace,
    FunctionalLaplace,
    KronLaplace,
    ParametricLaplace,
)
from torch.utils.data import DataLoader

from laplace.curvature import BackPackGGN
from laplace.utils import FeatureExtractor, Kron
from laplace.curvature.curvature import CurvatureInterface


from laplace.utils.enums import Likelihood

__all__ = [
    'LLLaplace',
    'FullLLLaplace',
    'KronLLLaplace',
    'DiagLLLaplace',
    'FunctionalLLLaplace',
]


class LLLaplace(ParametricLaplace):
    """Baseclass for all last-layer Laplace approximations in this library.
    Subclasses specify the structure of the Hessian approximation.
    See `BaseLaplace` for the full interface.

    A Laplace approximation is represented by a MAP which is given by the
    `model` parameter and a posterior precision or covariance specifying
    a Gaussian distribution \\(\\mathcal{N}(\\theta_{MAP}, P^{-1})\\).
    Here, only the parameters of the last layer of the neural network
    are treated probabilistically.
    The goal of this class is to compute the posterior precision \\(P\\)
    which sums as
    \\[
        P = \\sum_{n=1}^N \\nabla^2_\\theta \\log p(\\mathcal{D}_n \\mid \\theta)
        \\vert_{\\theta_{MAP}} + \\nabla^2_\\theta \\log p(\\theta) \\vert_{\\theta_{MAP}}.
    \\]
    Every subclass implements different approximations to the log likelihood Hessians,
    for example, a diagonal one. The prior is assumed to be Gaussian and therefore we have
    a simple form for \\(\\nabla^2_\\theta \\log p(\\theta) \\vert_{\\theta_{MAP}} = P_0 \\).
    In particular, we assume a scalar or diagonal prior precision so that in
    all cases \\(P_0 = \\textrm{diag}(p_0)\\) and the structure of \\(p_0\\) can be varied.

    Parameters
    ----------
    model : torch.nn.Module or `laplace.utils.feature_extractor.FeatureExtractor`
    likelihood : {'classification', 'regression'}
        determines the log likelihood Hessian approximation
    sigma_noise : torch.Tensor or float, default=1
        observation noise for the regression setting; must be 1 for classification
    prior_precision : torch.Tensor or float, default=1
        prior precision of a Gaussian prior (= weight decay);
        can be scalar, per-layer, or diagonal in the most general case
    prior_mean : torch.Tensor or float, default=0
        prior mean of a Gaussian prior, useful for continual learning
    temperature : float, default=1
        temperature of the likelihood; lower temperature leads to more
        concentrated posterior and vice versa.
    enable_backprop: bool, default=False
        whether to enable backprop to the input `x` through the Laplace predictive.
        Useful for e.g. Bayesian optimization.
    backend : subclasses of `laplace.curvature.CurvatureInterface`
        backend for access to curvature/Hessian approximations
    last_layer_name: str, default=None
        name of the model's last layer, if None it will be determined automatically
    backend_kwargs : dict, default=None
        arguments passed to the backend on initialization, for example to
        set the number of MC samples for stochastic approximations.
    """

    def __init__(
        self,
        model,
        likelihood,
        sigma_noise=1.0,
        prior_precision=1.0,
        prior_mean=0.0,
        temperature=1.0,
        enable_backprop=False,
        backend=None,
        last_layer_name=None,
        backend_kwargs=None,
        asdl_fisher_kwargs=None,
    ):
        if asdl_fisher_kwargs is not None:
            raise ValueError('Last-layer Laplace does not support asdl_fisher_kwargs.')
        self.H = None
        super().__init__(
            model,
            likelihood,
            sigma_noise=sigma_noise,
            prior_precision=1.0,
            prior_mean=0.0,
            temperature=temperature,
            enable_backprop=enable_backprop,
            backend=backend,
            backend_kwargs=backend_kwargs,
        )
        self.model = FeatureExtractor(
            deepcopy(model),
            last_layer_name=last_layer_name,
            enable_backprop=enable_backprop,
        )
        if self.model.last_layer is None:
            self.mean = None
            self.n_params = None
            self.n_layers = None
            # ignore checks of prior mean setter temporarily, check on .fit()
            self._prior_precision = prior_precision
            self._prior_mean = prior_mean
        else:
            self.n_params = len(
                parameters_to_vector(self.model.last_layer.parameters())
            )
            self.n_layers = len(list(self.model.last_layer.parameters()))
            self.prior_precision = prior_precision
            self.prior_mean = prior_mean
            self.mean = self.prior_mean
            self._init_H()
        self._backend_kwargs['last_layer'] = True
        self._last_layer_name = last_layer_name

    def fit(self, train_loader, override=True):
        """Fit the local Laplace approximation at the parameters of the model.

        Parameters
        ----------
        train_loader : torch.data.utils.DataLoader
            each iterate is a training batch (X, y);
            `train_loader.dataset` needs to be set to access \\(N\\), size of the data set
        override : bool, default=True
            whether to initialize H, loss, and n_data again; setting to False is useful for
            online learning settings to accumulate a sequential posterior approximation.
        """
        if not override:
            raise ValueError(
                'Last-layer Laplace approximations do not support `override=False`.'
            )

        self.model.eval()

        if self.model.last_layer is None:
            self.data = next(iter(train_loader))
            self._find_last_layer(self.data)
            params = parameters_to_vector(self.model.last_layer.parameters()).detach()
            self.n_params = len(params)
            self.n_layers = len(list(self.model.last_layer.parameters()))
            # here, check the already set prior precision again
            self.prior_precision = self._prior_precision
            self.prior_mean = self._prior_mean
            self._init_H()

        super().fit(train_loader, override=override)
        self.mean = parameters_to_vector(self.model.last_layer.parameters())

        if not self.enable_backprop:
            self.mean = self.mean.detach()

    def _glm_predictive_distribution(self, X, joint=False):
        Js, f_mu = self.backend.last_layer_jacobians(X)

        if joint:
            f_mu = f_mu.flatten()  # (batch*out)
            f_var = self.functional_covariance(Js)  # (batch*out, batch*out)
        else:
            f_var = self.functional_variance(Js)

        return (
            (f_mu.detach(), f_var.detach())
            if not self.enable_backprop
            else (f_mu, f_var)
        )

    def _nn_predictive_samples(self, X, n_samples=100, generator=None, **model_kwargs):
        fs = list()
        for sample in self.sample(n_samples, generator):
            vector_to_parameters(sample, self.model.last_layer.parameters())
            f = self.model(X.to(self._device), **model_kwargs)
            fs.append(f.detach() if not self.enable_backprop else f)
        vector_to_parameters(self.mean, self.model.last_layer.parameters())
        fs = torch.stack(fs)
        if self.likelihood == 'classification':
            fs = torch.softmax(fs, dim=-1)
        return fs

    def _nn_predictive_classification(
        self, X, n_samples=100, generator=None, **model_kwargs
    ):
        py = 0
        for sample in self.sample(n_samples, generator):
            vector_to_parameters(sample, self.model.last_layer.parameters())
            # TODO: Implement with a single forward pass until last layer.
            logits = self.model(X.to(self._device), **model_kwargs).detach()
            py += torch.softmax(logits, dim=-1) / n_samples
        vector_to_parameters(self.mean, self.model.last_layer.parameters())
        return py

    @property
    def prior_precision_diag(self):
        """Obtain the diagonal prior precision \\(p_0\\) constructed from either
        a scalar or diagonal prior precision.

        Returns
        -------
        prior_precision_diag : torch.Tensor
        """
        if len(self.prior_precision) == 1:  # scalar
            return self.prior_precision * torch.ones_like(self.mean)

        elif len(self.prior_precision) == self.n_params:  # diagonal
            return self.prior_precision

        else:
            raise ValueError('Mismatch of prior and model. Diagonal or scalar prior.')

    def state_dict(self) -> dict:
        state_dict = super().state_dict()
        state_dict['data'] = getattr(self, 'data', None)  # None if not present
        state_dict['_last_layer_name'] = self._last_layer_name
        return state_dict

    def load_state_dict(self, state_dict: dict):
        if self._last_layer_name != state_dict['_last_layer_name']:
            raise ValueError('Different `last_layer_name` detected!')

        self.data = state_dict['data']
        if self.data is not None:
            self._find_last_layer(self.data)

        super().load_state_dict(state_dict)

        params = parameters_to_vector(self.model.last_layer.parameters()).detach()
        self.n_params = len(params)
        self.n_layers = len(list(self.model.last_layer.parameters()))

    @torch.no_grad()
    def _find_last_layer(self, data: Union[torch.Tensor, MutableMapping]) -> None:
        # To support Huggingface dataset
        if isinstance(data, MutableMapping):
            self.model.find_last_layer(data)
        else:
            X = data[0]
            try:
                self.model.find_last_layer(X[:1].to(self._device))
            except (TypeError, AttributeError):
                self.model.find_last_layer(X.to(self._device))


class FullLLLaplace(LLLaplace, FullLaplace):
    """Last-layer Laplace approximation with full, i.e., dense, log likelihood Hessian approximation
    and hence posterior precision. Based on the chosen `backend` parameter, the full
    approximation can be, for example, a generalized Gauss-Newton matrix.
    Mathematically, we have \\(P \\in \\mathbb{R}^{P \\times P}\\).
    See `FullLaplace`, `LLLaplace`, and `BaseLaplace` for the full interface.
    """

    # key to map to correct subclass of BaseLaplace, (subset of weights, Hessian structure)
    _key = ('last_layer', 'full')


class KronLLLaplace(LLLaplace, KronLaplace):
    """Last-layer Laplace approximation with Kronecker factored log likelihood Hessian approximation
    and hence posterior precision.
    Mathematically, we have for the last parameter group, i.e., torch.nn.Linear,
    that \\P\\approx Q \\otimes H\\.
    See `KronLaplace`, `LLLaplace`, and `BaseLaplace` for the full interface and see
    `laplace.utils.matrix.Kron` and `laplace.utils.matrix.KronDecomposed` for the structure of
    the Kronecker factors. `Kron` is used to aggregate factors by summing up and
    `KronDecomposed` is used to add the prior, a Hessian factor (e.g. temperature),
    and computing posterior covariances, marginal likelihood, etc.
    Use of `damping` is possible by initializing or setting `damping=True`.
    """

    # key to map to correct subclass of BaseLaplace, (subset of weights, Hessian structure)
    _key = ('last_layer', 'kron')

    def __init__(
        self,
        model,
        likelihood,
        sigma_noise=1.0,
        prior_precision=1.0,
        prior_mean=0.0,
        temperature=1.0,
        enable_backprop=False,
        backend=None,
        last_layer_name=None,
        damping=False,
        **backend_kwargs,
    ):
        self.damping = damping
        super().__init__(
            model,
            likelihood,
            sigma_noise,
            prior_precision,
            prior_mean,
            temperature,
            enable_backprop,
            backend,
            last_layer_name,
            backend_kwargs,
        )

    def _init_H(self):
        self.H = Kron.init_from_model(self.model.last_layer, self._device)


class DiagLLLaplace(LLLaplace, DiagLaplace):
    """Last-layer Laplace approximation with diagonal log likelihood Hessian approximation
    and hence posterior precision.
    Mathematically, we have \\(P \\approx \\textrm{diag}(P)\\).
    See `DiagLaplace`, `LLLaplace`, and `BaseLaplace` for the full interface.
    """

    # key to map to correct subclass of BaseLaplace, (subset of weights, Hessian structure)
    _key = ('last_layer', 'diag')


class FunctionalLLLaplace(FunctionalLaplace):
    """Here not much changes in terms of GP inference compared to FunctionalLaplace class.
    Since now we treat only the last layer probabilistically and the rest of the network is used as a "fixed feature
    extractor", that means that the \\(X \in \mathbb{R}^{M \\times D}\\) in GP inference changes
    to \\(\\tilde{X} \\in \mathbb{R}^{M \\times l_{n-1}} \\),  where \\(l_{n-1}\\) is the dimension of the output
    of the penultimate NN layer.

    See `FunctionalLaplace` for the full interface.
    """

    # key to map to correct subclass of BaseLaplace, (subset of weights, Hessian structure)
    _key = ('last_layer', 'gp')

    def __init__(
        self,
        model: nn.Module,
        likelihood: Likelihood | str,
        num_data: int,
        sigma_noise: float | torch.Tensor = 1.0,
        prior_precision: float | torch.Tensor = 1.0,
        prior_mean: float | torch.Tensor = 0.0,
        temperature: float = 1.0,
        enable_backprop: bool = False,
        last_layer_name=None,
        backend: type[CurvatureInterface] | None = BackPackGGN,
        backend_kwargs: dict[str, Any] | None = None,
        diagonal_kernel: bool = False,
        seed: int = 0,
    ):
        super().__init__(
            model,
            likelihood,
            num_data=num_data,
            sigma_noise=sigma_noise,
            prior_precision=prior_precision,
            prior_mean=0.0,
            temperature=temperature,
            backend=backend,
            enable_backprop=enable_backprop,
            backend_kwargs=backend_kwargs,
            diagonal_kernel=diagonal_kernel,
            seed=seed,
        )
        self._last_layer_name = last_layer_name
        self.model = FeatureExtractor(
            deepcopy(model),
            last_layer_name=last_layer_name,
            enable_backprop=enable_backprop,
        )
        if self.model.last_layer is None:
            self.n_params = None
            self.n_layers = None
            # ignore checks of prior mean setter temporarily, check on .fit()
            self._prior_precision = prior_precision
            self._prior_mean = prior_mean
        else:
            self.n_params = len(self.mean)
            self.n_layers = len(list(self.model.last_layer.parameters()))
            self.prior_precision = prior_precision
            self.prior_mean = prior_mean
        self._backend_kwargs['last_layer'] = True

    def fit(self, train_loader : DataLoader) -> None:
        """Fit the Laplace approximation of a GP posterior.

        Parameters
        ----------
        train_loader : torch.data.utils.DataLoader
            `train_loader.dataset` needs to be set to access \\(N\\), size of the data set
            `train_loader.batch_size` needs to be set to access \\(b\\) batch_size
        """
        self.model.eval()

        if self.model.last_layer is None:
            self.data = next(iter(train_loader))
            with torch.no_grad():
                self._find_last_layer(self.data)
            self.mean = parameters_to_vector(
                self.model.last_layer.parameters()
            ).detach()
            self.n_params = len(self.mean)
            self.n_layers = len(list(self.model.last_layer.parameters()))
            # here, check the already set prior precision again
            self.prior_precision = self._prior_precision
            self.prior_mean = self._prior_mean

        super().fit(train_loader)

    def _jacobians(self, X : torch.Tensor) -> torch.Tensor:
        """
        A helper function to compute jacobians.
        """
        return self.backend.last_layer_jacobians(X, enable_backprop=self.enable_backprop)

    @torch.no_grad()
    def _find_last_layer(self, data: Union[torch.Tensor, MutableMapping]) -> None:
        # To support Huggingface dataset
        if isinstance(data, MutableMapping):
            self.model.find_last_layer(data)
        else:
            X = data[0]
            try:
                self.model.find_last_layer(X[:1].to(self._device))
            except (TypeError, AttributeError):
                self.model.find_last_layer(X.to(self._device))


    def state_dict(self) -> dict:
        state_dict = super().state_dict()
        state_dict['data'] = getattr(self, 'data', None)  # None if not present
        state_dict['_last_layer_name'] = self._last_layer_name
        return state_dict

    def load_state_dict(self, state_dict: dict):
        if self._last_layer_name != state_dict['_last_layer_name']:
            raise ValueError('Different `last_layer_name` detected!')

        self.data = state_dict['data']
        if self.data is not None:
            self._find_last_layer(self.data)

        super().load_state_dict(state_dict)

        params = parameters_to_vector(self.model.last_layer.parameters()).detach()
        self.n_params = len(params)
        self.n_layers = len(list(self.model.last_layer.parameters()))