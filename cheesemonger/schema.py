"""
Dataset schema definitions for cheesemonger.

A Dataset contains multiple DatatypeSpecs, each of which is an independently
stored array with its own set of dimensions. Datatype is a shard key, not a
dimension. 
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

    dim_sizes maps dimension name -> cardinality.
    datatypes lists every array shard in the dataset.
    append_dim is the dimension along which new chunks arrive incrementally.
    """

    name: str
    dim_sizes: dict[str, int]
    datatypes: tuple[DatatypeSpec, ...]
    append_dim: str = "screen"


# ---------------------------------------------------------------------------
# The PESCA dataset schema that matches the real data
# ---------------------------------------------------------------------------

PESCA_DIMS = ("screen", "timepoint", "testedperturbation", "testedgeneexpression")

PESCA_DATATYPES = (
    # 4-D: one value per (screen, timepoint, testedperturbation, testedgeneexpression)
    DatatypeSpec("MeanDifference", PESCA_DIMS),
    DatatypeSpec("DetrendedMeanDifference", PESCA_DIMS),
    DatatypeSpec("L2FC", PESCA_DIMS),
    DatatypeSpec("STD", PESCA_DIMS),
    DatatypeSpec("ZScore", PESCA_DIMS),
    DatatypeSpec("PermutationP", PESCA_DIMS),
    DatatypeSpec("FDR", PESCA_DIMS),
    DatatypeSpec("neg_log10_FDR", PESCA_DIMS),

    # 4-D but only varies by (screen, timepoint, testedperturbation)
    DatatypeSpec("nNonzeroTestCells", PESCA_DIMS),
    DatatypeSpec("TestMean", PESCA_DIMS),
    DatatypeSpec("nTestCells", PESCA_DIMS),
    DatatypeSpec("nPermutations", PESCA_DIMS),

    # 4-D but only varies by (screen, timepoint)
    DatatypeSpec("nNonzeroCtrlCells", PESCA_DIMS),
    DatatypeSpec("CtrlMean", PESCA_DIMS),
    DatatypeSpec("nCtrlCells", PESCA_DIMS),
)

# Future variant: store each datatype at its true dimensionality to reduce
# storage on duplicated values along unused axes.
PESCA_3D_DIMS = ("screen", "timepoint", "testedperturbation")
PESCA_2D_DIMS = ("screen", "timepoint")

PESCA_DATATYPES_REDUCED = (
    DatatypeSpec("MeanDifference", PESCA_DIMS),
    DatatypeSpec("DetrendedMeanDifference", PESCA_DIMS),
    DatatypeSpec("L2FC", PESCA_DIMS),
    DatatypeSpec("STD", PESCA_DIMS),
    DatatypeSpec("ZScore", PESCA_DIMS),
    DatatypeSpec("PermutationP", PESCA_DIMS),
    DatatypeSpec("FDR", PESCA_DIMS),
    DatatypeSpec("neg_log10_FDR", PESCA_DIMS),
    DatatypeSpec("nNonzeroTestCells", PESCA_3D_DIMS),
    DatatypeSpec("TestMean", PESCA_3D_DIMS),
    DatatypeSpec("nTestCells", PESCA_3D_DIMS),
    DatatypeSpec("nPermutations", PESCA_3D_DIMS),
    DatatypeSpec("nNonzeroCtrlCells", PESCA_2D_DIMS),
    DatatypeSpec("CtrlMean", PESCA_2D_DIMS),
    DatatypeSpec("nCtrlCells", PESCA_2D_DIMS),
)


def pesca_schema(
    n_screens: int = 30,
    n_timepoints: int = 2,
    n_testedperturbations: int = 10_000,
    n_testedgeneexpressions: int = 18_000,
) -> DatasetSchema:
    return DatasetSchema(
        name="pesca_simulated",
        dim_sizes={
            "screen": n_screens,
            "timepoint": n_timepoints,
            "testedperturbation": n_testedperturbations,
            "testedgeneexpression": n_testedgeneexpressions,
        },
        datatypes=PESCA_DATATYPES,
        append_dim="screen",
    )
