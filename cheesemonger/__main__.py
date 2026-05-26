"""Allow running subcommands via `python -m cheesemonger`."""

import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m cheesemonger <command>")
        print("Commands: simulate, benchmark")
        sys.exit(1)

    command = sys.argv[1]
    sys.argv = sys.argv[1:]  # shift argv so subcommand sees clean args

    if command == "simulate":
        from cheesemonger.simulate import main as sim_main
        sim_main()
    elif command == "benchmark":
        from cheesemonger.benchmark import main as bench_main
        bench_main()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
