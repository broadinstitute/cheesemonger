"""
Benchmark query performance for cheesemonger datasets.

Usage:
    python -m cheesemonger benchmark --help

Runs each query pattern multiple times and reports latency statistics
(min, median, p95, p99, max) for both cold and warm reads.

Supports a ``--concurrent`` mode that fires queries from multiple threads
simultaneously to test throughput and lock contention.

Works with the single-store model where each format produces one file:
    data/simulated/pesca_simulated.zarr
    data/simulated/pesca_simulated.nc
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from cheesemonger.query import Aggregate, get_vector, open_store
from cheesemonger.simulate import CHUNK_PRESETS, SCALE_PRESETS, chunk_sizes

logger = logging.getLogger(__name__)

# HDF5 (used by NetCDF4) is not thread-safe and will segfault under
# concurrent access.  This lock serialises NetCDF reads so the benchmark
# can still run — the serialisation itself is part of what we measure.
_netcdf_lock = threading.Lock()


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
# Concurrent benchmark
# ---------------------------------------------------------------------------

@dataclass
class ConcurrentResult:
    """Results from one concurrency-level run."""
    concurrency: int
    fmt: str
    total_queries: int
    wall_clock_s: float
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def throughput_qps(self) -> float:
        return self.total_queries / self.wall_clock_s if self.wall_clock_s > 0 else 0

    @property
    def p50_ms(self) -> float:
        return float(np.percentile(self.latencies_ms, 50))

    @property
    def p95_ms(self) -> float:
        return float(np.percentile(self.latencies_ms, 95))

    @property
    def p99_ms(self) -> float:
        return float(np.percentile(self.latencies_ms, 99))


def _timed_query(
    store_path: Path | str,
    fmt: str,
    spec: QuerySpec,
) -> float:
    """Execute one query and return its latency in ms.

    For NetCDF, acquires ``_netcdf_lock`` to prevent HDF5 segfaults.
    The time waiting on the lock is included in the latency — this
    faithfully reflects the cost of HDF5's lack of thread-safety.
    """
    t0 = time.perf_counter()
    if fmt == "netcdf":
        with _netcdf_lock:
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
    else:
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
    return (time.perf_counter() - t0) * 1000


def run_concurrent_benchmark(
    store_path: Path | str,
    fmt: str,
    concurrency_levels: list[int],
    queries_per_level: int = 40,
) -> list[ConcurrentResult]:
    """
    Fire queries from multiple threads and measure throughput + latency.

    For each concurrency level, submits *queries_per_level* queries drawn
    round-robin from the series query specs.  Each thread opens its own
    store handle (mimicking independent API requests).
    """
    ds = open_store(store_path, fmt)
    screens = ds.coords["screen"].values
    timepoints = ds.coords["timepoint"].values
    perturbations = ds.coords["testedperturbation"].values
    ds.close()

    screen = str(screens[0])
    timepoint = int(timepoints[0])
    perturbation = str(perturbations[len(perturbations) // 2])

    all_specs = build_query_specs(screen, timepoint, perturbation)
    series_specs = [s for s in all_specs if s.name.startswith("series/")]

    results: list[ConcurrentResult] = []

    for n_threads in concurrency_levels:
        # Build a work list cycling through series queries
        work = [series_specs[i % len(series_specs)] for i in range(queries_per_level)]

        # Warmup: run one query sequentially to prime caches / connections
        _timed_query(store_path, fmt, series_specs[0])

        latencies: list[float] = []
        wall_start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futures = [
                pool.submit(_timed_query, store_path, fmt, spec)
                for spec in work
            ]
            for fut in as_completed(futures):
                latencies.append(fut.result())

        wall_elapsed = time.perf_counter() - wall_start

        cr = ConcurrentResult(
            concurrency=n_threads,
            fmt=fmt,
            total_queries=queries_per_level,
            wall_clock_s=wall_elapsed,
            latencies_ms=latencies,
        )
        results.append(cr)
        logger.info(
            "  threads=%d  queries=%d  wall=%.2fs  throughput=%.1f q/s  "
            "p50=%.1fms  p95=%.1fms  p99=%.1fms",
            n_threads, queries_per_level, wall_elapsed,
            cr.throughput_qps, cr.p50_ms, cr.p95_ms, cr.p99_ms,
        )

    return results


def print_concurrent_results(
    results: list[ConcurrentResult],
    chunk_preset: str = "big",
) -> None:
    """Print a formatted table of concurrent benchmark results."""
    preset = CHUNK_PRESETS[chunk_preset]
    chunk_desc = (
        f"(1, 1, {preset['testedperturbation']}, {preset['testedgeneexpression']})"
    )
    chunk_mb = 1 * 1 * preset["testedperturbation"] * preset["testedgeneexpression"] * 4 / (1024 ** 2)

    print()
    print(f"Concurrent benchmark  |  chunk: {chunk_preset} {chunk_desc} (~{chunk_mb:.1f} MB)")
    print("=" * 110)
    print(
        f"{'Format':>8s}  {'Threads':>7s}  {'Queries':>7s}  "
        f"{'Wall(s)':>8s}  {'Q/s':>8s}  "
        f"{'P50':>10s}  {'P95':>10s}  {'P99':>10s}"
    )
    print("-" * 110)
    for cr in results:
        print(
            f"[{cr.fmt:>6s}]  {cr.concurrency:>7d}  {cr.total_queries:>7d}  "
            f"{cr.wall_clock_s:>8.2f}  {cr.throughput_qps:>8.1f}  "
            f"{cr.p50_ms:>8.1f}ms  {cr.p95_ms:>8.1f}ms  {cr.p99_ms:>8.1f}ms"
        )
    print("=" * 110)
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
    parser.add_argument(
        "--concurrent",
        nargs="*",
        type=int,
        metavar="N",
        help="Run concurrent benchmark at the given thread counts. "
             "E.g. --concurrent 1 2 4 8. If given without values, defaults to 1 2 4 8.",
    )
    parser.add_argument(
        "--concurrent-queries",
        type=int,
        default=40,
        help="Total queries to issue per concurrency level (default: 40)",
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

    # Decide which mode to run
    run_sequential = args.concurrent is None
    concurrency_levels = (
        args.concurrent if args.concurrent else [1, 2, 4, 8]
    )

    if run_sequential:
        # ── Standard sequential benchmark ──────────────────────────────
        all_results: list[BenchmarkResult] = []
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
    else:
        # ── Concurrent benchmark ───────────────────────────────────────
        all_concurrent: list[ConcurrentResult] = []
        for fmt in formats_to_test:
            store_path = fmt_paths[fmt]
            if not is_gcs and not Path(str(store_path)).exists():
                logger.warning("Store not found at %s — skipping %s", store_path, fmt)
                continue

            logger.info("Concurrent benchmark: %s at %s", fmt.upper(), store_path)
            cresults = run_concurrent_benchmark(
                store_path=store_path,
                fmt=fmt,
                concurrency_levels=concurrency_levels,
                queries_per_level=args.concurrent_queries,
            )
            all_concurrent.extend(cresults)

        if all_concurrent:
            print_concurrent_results(all_concurrent, chunk_preset=args.chunk_preset)
        else:
            logger.error("No datasets found. Run 'python3 -m cheesemonger simulate' first.")


if __name__ == "__main__":
    main()
