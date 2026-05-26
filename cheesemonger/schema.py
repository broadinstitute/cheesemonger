"""
Dataset schema definitions for cheesemonger.

A Dataset contains multiple DatatypeSpecs, each of which is an independently
stored array with its own set of dimensions. Datatype is a shard key, not a
dimension.

Screen is an *organizational key*, not a dimension within each array. Each
screen is stored as a separate Zarr store or NetCDF file, making screen-level
operations (add, delete, replace) cheap and independent.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DatatypeSpec:
    name: str
    dimensions: tuple[str, ...]
    dtype: str = "float32"


@dataclass(frozen=True)
class DatasetSchema:
    """
    Full schema for a dataset.

    dim_sizes maps dimension name -> cardinality for the dimensions *within*
    each per-screen store.  Screen is not listed here — it is the organizational
    axis that determines which store to open.

    datatypes lists every array shard in the dataset.
    """

    name: str
    dim_sizes: dict[str, int]
    datatypes: tuple[DatatypeSpec, ...]
    n_screens: int = 30
    screen_prefix: str = "Screen"


# ---------------------------------------------------------------------------
# The PESCA dataset schema that matches the real data
# ---------------------------------------------------------------------------

PESCA_DIMS = ("timepoint", "testedperturbation", "testedgeneexpression")

PESCA_DATATYPES = (
    # 3-D (per screen): one value per (timepoint, testedperturbation, testedgeneexpression)
    DatatypeSpec("MeanDifference", PESCA_DIMS),
    DatatypeSpec("DetrendedMeanDifference", PESCA_DIMS),
    DatatypeSpec("L2FC", PESCA_DIMS),
    DatatypeSpec("STD", PESCA_DIMS),
    DatatypeSpec("ZScore", PESCA_DIMS),
    DatatypeSpec("PermutationP", PESCA_DIMS),
    DatatypeSpec("FDR", PESCA_DIMS),
    DatatypeSpec("neg_log10_FDR", PESCA_DIMS),

    # Effectively 2-D (timepoint, testedperturbation) — constant across
    # testedgeneexpression.  Stored as 3-D with duplicated values for now.
    DatatypeSpec("nNonzeroTestCells", PESCA_DIMS),
    DatatypeSpec("TestMean", PESCA_DIMS),
    DatatypeSpec("nTestCells", PESCA_DIMS),
    DatatypeSpec("nPermutations", PESCA_DIMS),

    # Effectively 1-D (timepoint) — constant across both perturbation axes.
    DatatypeSpec("nNonzeroCtrlCells", PESCA_DIMS),
    DatatypeSpec("CtrlMean", PESCA_DIMS),
    DatatypeSpec("nCtrlCells", PESCA_DIMS),
)

# Future variant: store each datatype at its true dimensionality to reduce
# storage on duplicated values along unused axes.
PESCA_2D_DIMS = ("timepoint", "testedperturbation")
PESCA_1D_DIMS = ("timepoint",)

PESCA_DATATYPES_REDUCED = (
    DatatypeSpec("MeanDifference", PESCA_DIMS),
    DatatypeSpec("DetrendedMeanDifference", PESCA_DIMS),
    DatatypeSpec("L2FC", PESCA_DIMS),
    DatatypeSpec("STD", PESCA_DIMS),
    DatatypeSpec("ZScore", PESCA_DIMS),
    DatatypeSpec("PermutationP", PESCA_DIMS),
    DatatypeSpec("FDR", PESCA_DIMS),
    DatatypeSpec("neg_log10_FDR", PESCA_DIMS),
    DatatypeSpec("nNonzeroTestCells", PESCA_2D_DIMS),
    DatatypeSpec("TestMean", PESCA_2D_DIMS),
    DatatypeSpec("nTestCells", PESCA_2D_DIMS),
    DatatypeSpec("nPermutations", PESCA_2D_DIMS),
    DatatypeSpec("nNonzeroCtrlCells", PESCA_1D_DIMS),
    DatatypeSpec("CtrlMean", PESCA_1D_DIMS),
    DatatypeSpec("nCtrlCells", PESCA_1D_DIMS),
)


def pesca_schema(
    n_screens: int = 30,
    n_timepoints: int = 2,
    n_testedperturbations: int = 10_000,
    n_testedgeneexpressions: int = 18_000,
) -> DatasetSchema:
    return DatasetSchema(
        name="pesca",
        dim_sizes={
            "timepoint": n_timepoints,
            "testedperturbation": n_testedperturbations,
            "testedgeneexpression": n_testedgeneexpressions,
        },
        datatypes=PESCA_DATATYPES,
        n_screens=n_screens,
    )
