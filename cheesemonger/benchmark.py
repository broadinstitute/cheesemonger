"""
Benchmark query performance for cheesemonger datasets.

Usage:
    python -m cheesemonger benchmark --help

Runs each query pattern multiple times and reports latency statistics
(min, median, p95, p99, max) for both cold and warm reads.

Works with the single-store model where each format produces one file:
    data/simulated/pesca_simulated.zarr
    data/simulated/pesca_simulated.nc
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cheesemonger.query import Aggregate, get_vector, open_store
from cheesemonger.simulate import CHUNK_PRESETS, SCALE_PRESETS, chunk_sizes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Benchmark result container
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkResult:
    name: str
    fmt: str
    latencies_ms: list[float]
    result_shape: tuple[int, ...]

    @property
    def min_ms(self) -> float:
        return float(np.min(self.latencies_ms))

    @property
    def median_ms(self) -> float:
        return float(np.median(self.latencies_ms))

    @property
    def p95_ms(self) -> float:
        return float(np.percentile(self.latencies_ms, 95))

    @property
    def p99_ms(self) -> float:
        return float(np.percentile(self.latencies_ms, 99))

    @property
    def max_ms(self) -> float:
        return float(np.max(self.latencies_ms))

    def summary_line(self) -> str:
        return (
            f"{self.name:<40s} [{self.fmt:>6s}]  "
            f"min={self.min_ms:>8.1f}ms  "
            f"p50={self.median_ms:>8.1f}ms  "
            f"p95={self.p95_ms:>8.1f}ms  "
            f"p99={self.p99_ms:>8.1f}ms  "
            f"max={self.max_ms:>8.1f}ms  "
            f"shape={self.result_shape}"
        )


# ---------------------------------------------------------------------------
# Query definitions
# ---------------------------------------------------------------------------

@dataclass
class QuerySpec:
    """Describes one benchmark query pattern."""
    name: str
    datatype: str
    constraints: dict
    aggregate: Aggregate = Aggregate.NONE
    aggregate_over: str | None = None
    aggregate_threshold: float | None = None
    diagonal: tuple[str, str] | None = None


def build_query_specs(
    screen: str,
    timepoint: int,
    perturbation: str,
) -> list[QuerySpec]:
    """Build the full set of benchmark queries using concrete coordinate values."""
    fixed_3 = {"screen": screen, "timepoint": timepoint, "testedperturbation": perturbation}
    fixed_2 = {"screen": screen, "timepoint": timepoint}

    return [
        # --- Series queries (fix screen+timepoint+perturbation, read all gene expressions) ---
        QuerySpec(
            name="series/ZScore",
            datatype="ZScore",
            constraints=fixed_3,
        ),
        QuerySpec(
            name="series/neg_log10_FDR",
            datatype="neg_log10_FDR",
            constraints=fixed_3,
        ),
        QuerySpec(
            name="series/L2FC",
            datatype="L2FC",
            constraints=fixed_3,
        ),
        QuerySpec(
            name="series/TestMean",
            datatype="TestMean",
            constraints=fixed_3,
        ),
        QuerySpec(
            name="series/CtrlMean",
            datatype="CtrlMean",
            constraints=fixed_3,
        ),
        QuerySpec(
            name="series/nTestCells",
            datatype="nTestCells",
            constraints=fixed_3,
        ),

        # --- Aggregation queries ---
        QuerySpec(
            name="agg/ZScore_mean_over_perturbation",
            datatype="ZScore",
            constraints=fixed_2,
            aggregate=Aggregate.MEAN,
            aggregate_over="testedperturbation",
        ),
        QuerySpec(
            name="agg/FDR_count_lt_0.1",
            datatype="FDR",
            constraints=fixed_2,
            aggregate=Aggregate.COUNT_LT,
            aggregate_over="testedgeneexpression",
            aggregate_threshold=0.1,
        ),

        # Diagonal query (self-targeting: "when Gene X was knocked out, how
        # did Gene X's expression change?") is disabled until real data is
        # available. Simulated data uses different label prefixes for the two
        # dimensions so the diagonal is always empty.
        #
        # QuerySpec(
        #     name="diagonal/L2FC_self_targeting",
        #     datatype="L2FC",
        #     constraints=fixed_2,
        #     diagonal=("testedperturbation", "testedgeneexpression"),
        # ),
    ]


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_one_query(
    store_path: Path | str,
    fmt: str,
    spec: QuerySpec,
    n_iterations: int,
    warmup: int = 1,
) -> BenchmarkResult:
    """Run a single query spec multiple times and collect latencies."""
    for _ in range(warmup):
        get_vector(
            store_path=store_path,
            fmt=fmt,
            datatype=spec.datatype,
            constraints=spec.constraints,
            aggregate=spec.aggregate,
            aggregate_over=spec.aggregate_over,
            aggregate_threshold=spec.aggregate_threshold,
            diagonal=spec.diagonal,
        )

    latencies: list[float] = []
    result_shape: tuple[int, ...] = ()

    for _ in range(n_iterations):
        t0 = time.perf_counter()
        result = get_vector(
            store_path=store_path,
            fmt=fmt,
            datatype=spec.datatype,
            constraints=spec.constraints,
            aggregate=spec.aggregate,
            aggregate_over=spec.aggregate_over,
            aggregate_threshold=spec.aggregate_threshold,
            diagonal=spec.diagonal,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed_ms)
        result_shape = result.shape

    return BenchmarkResult(
        name=spec.name,
        fmt=fmt,
        latencies_ms=latencies,
        result_shape=result_shape,
    )


def run_benchmark(
    store_path: Path | str,
    fmt: str,
    n_iterations: int = 10,
    warmup: int = 1,
) -> list[BenchmarkResult]:
    """Run all benchmark queries against a dataset store."""
    ds = open_store(store_path, fmt)
    screens = ds.coords["screen"].values
    timepoints = ds.coords["timepoint"].values
    perturbations = ds.coords["testedperturbation"].values
    ds.close()

    screen = str(screens[0])
    timepoint = int(timepoints[0])
    perturbation = str(perturbations[len(perturbations) // 2])

    logger.info(
        "Query params: screen=%s, timepoint=%s, perturbation=%s",
        screen, timepoint, perturbation,
    )

    specs = build_query_specs(screen, timepoint, perturbation)
    results: list[BenchmarkResult] = []

    for spec in specs:
        logger.info("Running: %s (%d warmup + %d measured)", spec.name, warmup, n_iterations)
        result = run_one_query(store_path, fmt, spec, n_iterations, warmup)
        logger.info("  %s", result.summary_line())
        results.append(result)

    return results


def print_results(results: list[BenchmarkResult], chunk_preset: str = "big") -> None:
    """Print a formatted summary table of benchmark results."""
    preset = CHUNK_PRESETS[chunk_preset]
    chunk_desc = (
        f"(1, 1, {preset['testedperturbation']}, {preset['testedgeneexpression']})"
    )
    chunk_mb = 1 * 1 * preset["testedperturbation"] * preset["testedgeneexpression"] * 4 / (1024 ** 2)

    print()
    print(f"Chunk preset: {chunk_preset}  shape: {chunk_desc}  (~{chunk_mb:.1f} MB per chunk)")
    print("=" * 120)
    print(f"{'Query':<40s} {'Format':>8s}  {'Min':>10s}  {'P50':>10s}  {'P95':>10s}  {'P99':>10s}  {'Max':>10s}  Shape")
    print("-" * 120)
    for r in results:
        print(
            f"{r.name:<40s} [{r.fmt:>6s}]  "
            f"{r.min_ms:>8.1f}ms  "
            f"{r.median_ms:>8.1f}ms  "
            f"{r.p95_ms:>8.1f}ms  "
            f"{r.p99_ms:>8.1f}ms  "
            f"{r.max_ms:>8.1f}ms  "
            f"{r.result_shape}"
        )
    print("=" * 120)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Benchmark query performance on simulated cheesemonger datasets."
    )
    parser.add_argument(
        "--data-dir",
        default="data/simulated",
        help="Directory containing generated datasets (default: data/simulated). "
             "Supports gs:// URIs for Zarr on GCS.",
    )
    parser.add_argument(
        "--format",
        choices=["zarr", "netcdf", "both"],
        default="both",
        help="Which format(s) to benchmark (default: both)",
    )
    parser.add_argument(
        "--dataset-name",
        default="pesca_simulated",
        help="Dataset base name (default: pesca_simulated)",
    )
    parser.add_argument(
        "--scale",
        choices=list(SCALE_PRESETS.keys()),
        default="small",
        help="Scale used when generating data (default: small). "
             "Appended to dataset name in filenames.",
    )
    parser.add_argument(
        "--chunk-preset",
        choices=list(CHUNK_PRESETS.keys()),
        default="big",
        help="Chunk preset used when generating data (default: big). "
             "Determines which subdirectory to read from.",
    )
    parser.add_argument(
        "-n", "--iterations",
        type=int,
        default=10,
        help="Number of measured iterations per query (default: 10)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Number of warmup iterations before measurement (default: 1)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    logging.getLogger("numcodecs").setLevel(logging.WARNING)
    logging.getLogger("zarr").setLevel(logging.WARNING)
    logging.getLogger("gcsfs").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("fsspec").setLevel(logging.WARNING)

    formats_to_test = ["zarr", "netcdf"] if args.format == "both" else [args.format]
    all_results: list[BenchmarkResult] = []

    data_dir = args.data_dir
    is_gcs = isinstance(data_dir, str) and data_dir.startswith("gs://")

    ds_name = f"{args.dataset_name}_{args.scale}"

    if is_gcs:
        chunk_dir = f"{data_dir.rstrip('/')}/chunks_{args.chunk_preset}"
        fmt_paths: dict[str, Path | str] = {
            "zarr": f"{chunk_dir}/{ds_name}.zarr",
            "netcdf": f"{chunk_dir}/{ds_name}.nc",
        }
    else:
        local_chunk = Path(data_dir) / f"chunks_{args.chunk_preset}"
        fmt_paths = {
            "zarr": local_chunk / f"{ds_name}.zarr",
            "netcdf": local_chunk / f"{ds_name}.nc",
        }

    for fmt in formats_to_test:
        store_path = fmt_paths[fmt]
        if not is_gcs and not Path(str(store_path)).exists():
            logger.warning("Store not found at %s — skipping %s", store_path, fmt)
            continue

        logger.info("Benchmarking %s at %s", fmt.upper(), store_path)
        results = run_benchmark(
            store_path=store_path,
            fmt=fmt,
            n_iterations=args.iterations,
            warmup=args.warmup,
        )
        all_results.extend(results)

    if all_results:
        print_results(all_results, chunk_preset=args.chunk_preset)
    else:
        logger.error("No datasets found. Run 'python3 -m cheesemonger simulate' first.")


if __name__ == "__main__":
    main()
