from laplace.utils.feature_extractor import FeatureExtractor
from laplace.utils.matrix import Kron, KronDecomposed
from laplace.utils.metrics import RunningNLLMetric
from laplace.utils.subnetmask import (
    LargestMagnitudeSubnetMask,
    LargestVarianceDiagLaplaceSubnetMask,
    LargestVarianceSWAGSubnetMask,
    LastLayerSubnetMask,
    ModuleNameSubnetMask,
    ParamNameSubnetMask,
    RandomSubnetMask,
    SubnetMask,
)
from laplace.utils.swag import fit_diagonal_swag_var
from laplace.utils.utils import (
    _is_batchnorm,
    _is_valid_scalar,
    block_diag,
    diagonal_add_scalar,
    expand_prior_precision,
    fix_prior_prec_structure,
    get_nll,
    invsqrt_precision,
    kron,
    normal_samples,
    parameters_per_layer,
    symeig,
    validate,
)

__all__ = [
    "get_nll",
    "validate",
    "parameters_per_layer",
    "invsqrt_precision",
    "kron",
    "diagonal_add_scalar",
    "symeig",
    "block_diag",
    "normal_samples",
    "_is_batchnorm",
    "_is_valid_scalar",
    "expand_prior_precision",
    "fix_prior_prec_structure",
    "FeatureExtractor",
    "Kron",
    "KronDecomposed",
    "fit_diagonal_swag_var",
    "SubnetMask",
    "RandomSubnetMask",
    "LargestMagnitudeSubnetMask",
    "LargestVarianceDiagLaplaceSubnetMask",
    "LargestVarianceSWAGSubnetMask",
    "ParamNameSubnetMask",
    "ModuleNameSubnetMask",
    "LastLayerSubnetMask",
    "RunningNLLMetric",
]
