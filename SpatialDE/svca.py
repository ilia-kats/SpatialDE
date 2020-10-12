from time import time
from typing import Optional, List, Tuple, Union
import warnings
from dataclasses import dataclass, field

from tqdm.auto import tqdm, trange
from anndata import AnnData
import numpy as np
import pandas as pd
import gpflow
from gpflow.utilities import to_default_float
import tensorflow as tf
import tensorflow_probability as tfp

from ._internal.sm_kernel import Spectral, SpectralMixture
from ._internal.svca import SVCA, SVCAInteractionScoreTest
from ._internal.optimizer import MultiScipyOptimizer
from ._internal.util import get_l_limits, bh_adjust, calc_sizefactors
from ._internal.distance_cache import DistanceCache


def test_spatial_interactions(
    adata: AnnData,
    spatial_key: str = "spatial",
    ncomponents: int = 1,
    ard: bool = False,
    sizefactors: Optional[np.ndarray] = None,
    copy: bool = False,
) -> Tuple[pd.DataFrame, Union[AnnData, None]]:
    if ncomponents < 1:
        warnings.warn(
            f"ncomponents must be 1 or larger, but received {ncomponents}. Setting ncomponents=1"
        )
        ncomponents = 1

    if sizefactors is None:
        sizefactors = calc_sizefactors(adata)

    X = adata.obsm[spatial_key]
    l_min, l_max = get_l_limits(DistanceCache(X))
    kernels = []
    for l in np.geomspace(l_min, l_max, ncomponents):
        if ard:
            lengthscales = [l] * X.shape[1]
            periods = [l_max] * X.shape[1]
        else:
            lengthscales = l
            periods = l_max
        k = Spectral(variance=1, lengthscales=lengthscales, periods=periods)
        gpflow.set_trainable(k.variance, False)
        k.lengthscales.transform = tfp.bijectors.Sigmoid(
            low=to_default_float(0.5 * l_min), high=to_default_float(2 * l_max)
        )
        k.periods.transform = tfp.bijectors.Sigmoid(
            low=to_default_float(0.5 * l_min), high=to_default_float(2 * l_max)
        )
        kernels.append(k)
    kernel = SpectralMixture(kernels)

    results = []
    parameters = []
    test = SVCAInteractionScoreTest(adata.X, X, sizefactors, kernel)

    params = gpflow.utilities.parameter_dict(test.kernel[0])
    sortedkeys = sorted(params.keys())
    dtype = np.dtype([(k, params[k].dtype.as_numpy_dtype) for k in sortedkeys])

    for i, g in enumerate(tqdm(adata.var_names)):
        t0 = time()
        res, _ = test(i, None)
        t = time() - t0
        results.append({"time": t, "pval": res.pval.numpy(), "gene": g})
        params = gpflow.utilities.read_values(test.kernel[0])
        parameters.append(tuple([params[k] for k in sortedkeys]))

    results = pd.DataFrame(results)
    results.loc[
        results.pval > 1, "pval"
    ] = 1  # this seems to be a bug in tensorflow_probability, survival_function should never be >1
    results["padj"] = bh_adjust(results.pval.to_numpy())

    if copy:
        adata = adata.copy()
        toreturn = adata
    else:
        toreturn = None
    adata.varm["svca"] = np.array(parameters, dtype=dtype)
    adata.obsm["svca_sizefactors"] = sizefactors
    adata.uns["svca_ncomponents"] = ncomponents
    adata.uns["svca_ard"] = ard

    return results, toreturn


def fit_spatial_interactions(
    adata: AnnData,
    genes: Optional[List[str]],
    spatial_key: str = "spatial",
    ncomponents: int = 1,
    ard: bool = False,
    sizefactors: Optional[np.ndarray] = None,
) -> pd.DataFrame:
    if (
        "svca" not in adata.varm
        or "svca_sizefactors" not in adata.obsm
        or "svca_ncomponents" not in adata.uns
        or "svca_ard" not in adata.uns
    ):
        warnings.warn("SVCA parameters not found in adata. Performing ab initio fitting.")
        if genes is None:
            genes = adata.var_names
        if sizefactors is None:
            sizefactors = calc_sizefactors(adata)
        if ncomponents < 1:
            warnings.warn(
                f"ncomponents must be 1 or larger, but received {ncomponents}. Setting ncomponents=1"
            )
            ncomponents = 1
        trainable = True
    else:
        if genes is None:
            warnings.warn("No genes given. Fitting all genes.")
            genes = adata.var_names
        sizefactors = adata.obsm["svca_sizefactors"]
        ard = adata.uns["svca_ard"]
        ncomponents = adata.uns["svca_ncomponents"]
        trainable = False

    X = adata.obsm[spatial_key]

    kernels = []
    lengthscales = [1] * X.shape[1] if ard else 1
    periods = [1] * X.shape[1] if ard else 1
    for i in range(ncomponents):
        k = Spectral(lengthscales=lengthscales, periods=periods)
        gpflow.set_trainable(k.variance, False)
        gpflow.set_trainable(k.lengthscales, trainable)
        gpflow.set_trainable(k.periods, trainable)
        kernels.append(k)
    kernel = SpectralMixture(kernels)

    model = SVCA(adata.X, X, sizefactors, kernel)
    model.use_interactions(True)
    opt = MultiScipyOptimizer(lambda: -model.profile_log_reml(), model.trainable_variables)

    idx = np.argsort(adata.var_names)
    idx = idx[np.searchsorted(adata.var_names.to_numpy(), genes, sorter=idx)]
    if not trainable:
        param_names = adata.varm["svca"].dtype.names

    results = []
    for g, i in zip(tqdm(genes), idx):
        model.currentgene = i
        if not trainable:
            gpflow.utilities.multiple_assign(
                model.kernel, {n: v for n, v in zip(param_names, adata.varm["svca"][i])}
            )
        t0 = time()
        model.optimize()
        t = time() - t0
        fracvars = model.fraction_variance()._asdict()
        fracvars.update({"gene": g, "time": t})
        results.append(fracvars)
    return pd.DataFrame(results)
