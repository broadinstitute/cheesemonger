# Developer task shortcuts. These wrap the read-only CI gates so they can be
# run (and permission-allowlisted) as fixed commands instead of `uv run ...`.
#
#   make lint        # ruff lint
#   make typecheck   # pyright type check
#   make test        # pytest
#   make check       # all three (matches CI)

.PHONY: lint typecheck test check

lint:
	uv run ruff check cheesemonger tests

typecheck:
	uv run pyright cheesemonger

test:
	uv run pytest -q

check: lint typecheck test
