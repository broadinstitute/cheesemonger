from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

# Single source of truth for names that become filesystem path components
# (dataset, block, dimension, datatype, chunk dim). Allows letters, digits,
# underscore, and hyphen; the name must be non-empty and may start with a
# digit (real cell-line block names like "22Rv1" or "786-O" do). Slashes and
# dots are disallowed, which blocks path traversal: a block named ".." or
# "../etc" can never resolve outside its parent directory.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
_MAX_NAME_LEN = 128

MAX_DIMENSIONS = 20
MAX_LABELS_PER_DIMENSION = 50_000
MAX_DATATYPES = 100


class InvalidName(ValueError):
    """Raised when a name is not a safe filesystem path component.

    Subclasses ValueError so Pydantic treats it as a validation error (422)
    when raised inside a model. For URL path params (not Pydantic-validated),
    the service layer raises it at the path-construction chokepoint and the
    app's global handler maps it to a 400.
    """


def sanitize_name(value: str) -> str:
    """Validate that a name is safe to use as a filesystem path component.

    The one place name rules live. Used by the Pydantic schemas (request
    bodies) and the dataset service (URL path params + path construction).
    Never build a filesystem path from an outside string that hasn't passed
    through here.
    """
    if len(value) > _MAX_NAME_LEN or not _NAME_RE.match(value):
        raise InvalidName(
            f"Invalid name {value!r}: must be 1-{_MAX_NAME_LEN} characters of "
            f"letters, digits, underscores, or hyphens (no slashes or dots)."
        )
    return value


# Annotated str that runs sanitize_name during Pydantic validation.
SafeName = Annotated[str, AfterValidator(sanitize_name)]


class Dimension(BaseModel):
    name: SafeName
    labels: list[int] | list[str] = Field(max_length=MAX_LABELS_PER_DIMENSION)


class DatatypeSpec(BaseModel):
    name: SafeName
    dimensions: list[str]
    dtype: str = "float32"


class ChunkDim(BaseModel):
    name: SafeName
    size: int = Field(gt=0)
