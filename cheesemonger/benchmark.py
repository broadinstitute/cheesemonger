"""
Benchmark query performance for cheesemonger datasets.

Usage:
    python -m cheesemonger benchmark --help

Runs each query pattern multiple times and reports latency statistics
(min, median, p95, p99, max) for both cold and warm reads.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cheesemonger.query import Aggregate, get_vector, list_screens, open_screen
from cheesemonger.schema import pesca_schema
from cheesemonger.simulate import SCALE_PRESETS

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
    fixed_3 = {"timepoint": timepoint, "testedperturbation": perturbation}
    fixed_1 = {"timepoint": timepoint}

    return [
        # --- Series queries (fix 3 dims, read all gene expressions) ---
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
            constraints=fixed_1,
            aggregate=Aggregate.MEAN,
            aggregate_over="testedperturbation",
        ),
        QuerySpec(
            name="agg/FDR_count_lt_0.1",
            datatype="FDR",
            constraints=fixed_1,
            aggregate=Aggregate.COUNT_LT,
            aggregate_over="testedgeneexpression",
            aggregate_threshold=0.1,
        ),

        # --- Diagonal query ---
        QuerySpec(
            name="diagonal/L2FC_self_targeting",
            datatype="L2FC",
            constraints=fixed_1,
            diagonal=("testedperturbation", "testedgeneexpression"),
        ),
    ]


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_one_query(
    dataset_root: Path,
    fmt: str,
    screen: str,
    spec: QuerySpec,
    n_iterations: int,
    warmup: int = 1,
) -> BenchmarkResult:
    """Run a single query spec multiple times and collect latencies."""
    for _ in range(warmup):
        get_vector(
            dataset_root=dataset_root,
            fmt=fmt,
            screen=screen,
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
            dataset_root=dataset_root,
            fmt=fmt,
            screen=screen,
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
    dataset_root: Path,
    fmt: str,
    n_iterations: int = 10,
    warmup: int = 1,
) -> list[BenchmarkResult]:
    """Run all benchmark queries against a dataset."""
    screens = list_screens(dataset_root)
    if not screens:
        raise RuntimeError(f"No screens found in {dataset_root}")

    screen = screens[0]
    logger.info("Using screen: %s", screen)

    ds = open_screen(dataset_root, fmt, screen)
    timepoints = ds.coords["timepoint"].values
    perturbations = ds.coords["testedperturbation"].values
    ds.close()

    timepoint = int(timepoints[0])
    perturbation = str(perturbations[len(perturbations) // 2])

    logger.info(
        "Query params: timepoint=%s, perturbation=%s",
        timepoint, perturbation,
    )

    specs = build_query_specs(screen, timepoint, perturbation)
    results: list[BenchmarkResult] = []

    for spec in specs:
        logger.info("Running: %s (%d warmup + %d measured)", spec.name, warmup, n_iterations)
        result = run_one_query(dataset_root, fmt, screen, spec, n_iterations, warmup)
        logger.info("  %s", result.summary_line())
        results.append(result)

    return results


def print_results(results: list[BenchmarkResult]) -> None:
    """Print a formatted summary table of benchmark results."""
    print()
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
        type=Path,
        default=Path("data/simulated"),
        help="Base directory containing generated datasets (default: data/simulated)",
    )
    parser.add_argument(
        "--format",
        choices=["zarr", "netcdf", "both"],
        default="both",
        help="Which format(s) to benchmark (default: both)",
    )
    parser.add_argument(
        "--scale",
        choices=list(SCALE_PRESETS.keys()),
        default=None,
        help="Scale preset (used to locate the dataset directory). "
             "If omitted, auto-detects from data-dir.",
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
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    schema = pesca_schema(**(SCALE_PRESETS[args.scale] if args.scale else {}))
    dataset_name = schema.name

    formats_to_test = ["zarr", "netcdf"] if args.format == "both" else [args.format]
    all_results: list[BenchmarkResult] = []

    for fmt in formats_to_test:
        dataset_root = args.data_dir / dataset_name / fmt
        if not dataset_root.exists():
            logger.warning("Dataset not found at %s — skipping %s", dataset_root, fmt)
            continue

        logger.info("Benchmarking %s at %s", fmt.upper(), dataset_root)
        results = run_benchmark(
            dataset_root=dataset_root,
            fmt=fmt,
            n_iterations=args.iterations,
            warmup=args.warmup,
        )
        all_results.extend(results)

    if all_results:
        print_results(all_results)
    else:
        logger.error("No datasets found. Run 'python3 -m cheesemonger simulate' first.")


if __name__ == "__main__":
    main()
