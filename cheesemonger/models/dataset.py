"""SQLAlchemy ORM models for dataset and block metadata.

Dimensions, datatypes, and chunk_shape are stored as JSON columns. These are
complex nested structures (dimensions can have 50k+ labels) that don't benefit
from being individual rows — they're always read and written as a unit.
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..schemas.common import ChunkDimDict, DatatypeDict, DimensionDict
from .base import Base, UUIDMixin


class Dataset(Base, UUIDMixin):
    __tablename__ = "dataset"

    name: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    last_dimension: Mapped[str] = mapped_column(String, nullable=False)
    dimensions: Mapped[list[DimensionDict]] = mapped_column(JSON, nullable=False)
    datatypes: Mapped[list[DatatypeDict]] = mapped_column(JSON, nullable=False)
    chunk_shape: Mapped[list[ChunkDimDict]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    blocks: Mapped[list["Block"]] = relationship(back_populates="dataset_rel")


class Block(Base, UUIDMixin):
    __tablename__ = "block"
    __table_args__ = (
        UniqueConstraint("dataset_id", "name", name="uq_block_dataset_name"),
    )

    dataset_id: Mapped[str] = mapped_column(
        String, ForeignKey("dataset.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False, index=True)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    dataset_rel: Mapped["Dataset"] = relationship(back_populates="blocks")
