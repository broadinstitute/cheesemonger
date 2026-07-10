"""CRUD operations for dataset and block metadata in SQLite."""

from __future__ import annotations

from sqlalchemy.orm import Session

from cheesemonger.models.dataset import Block, Dataset
from cheesemonger.schemas.common import SchemaDict
from cheesemonger.schemas.dataset import DatasetIn


def get_dataset_by_name(db: Session, name: str) -> Dataset | None:
    return db.query(Dataset).filter(Dataset.name == name).first()


def dataset_exists(db: Session, name: str) -> bool:
    return get_dataset_by_name(db, name) is not None


def create_dataset(db: Session, dataset_in: DatasetIn) -> Dataset:
    ds = Dataset(
        name=dataset_in.name,
        last_dimension=dataset_in.last_dimension,
        dimensions=[d.model_dump() for d in dataset_in.dimensions],
        datatypes=[d.model_dump() for d in dataset_in.datatypes],
        chunk_shape=[c.model_dump() for c in dataset_in.chunk_shape],
    )
    db.add(ds)
    db.flush()
    return ds


def list_datasets(db: Session) -> list[Dataset]:
    return db.query(Dataset).order_by(Dataset.name).all()


def delete_dataset(db: Session, name: str) -> bool:
    ds = get_dataset_by_name(db, name)
    if ds is None:
        return False
    db.delete(ds)
    db.flush()
    return True


def get_schema_dict(db: Session, name: str) -> SchemaDict | None:
    """Return the dataset schema as a plain dict (for the query engine)."""
    ds = get_dataset_by_name(db, name)
    if ds is None:
        return None
    return {
        "name": ds.name,
        "last_dimension": ds.last_dimension,
        "dimensions": ds.dimensions,
        "datatypes": ds.datatypes,
        "chunk_shape": ds.chunk_shape,
    }


def list_block_names(db: Session, dataset_name: str) -> list[str]:
    ds = get_dataset_by_name(db, dataset_name)
    if ds is None:
        return []
    return sorted(b.name for b in ds.blocks)


def get_block(db: Session, dataset_name: str, block_name: str) -> Block | None:
    ds = get_dataset_by_name(db, dataset_name)
    if ds is None:
        return None
    return db.query(Block).filter(
        Block.dataset_id == ds.id, Block.name == block_name
    ).first()


def block_exists(db: Session, dataset_name: str, block_name: str) -> bool:
    return get_block(db, dataset_name, block_name) is not None


def create_block(db: Session, dataset_name: str, block_name: str) -> Block:
    ds = get_dataset_by_name(db, dataset_name)
    if ds is None:
        raise ValueError(f"Dataset {dataset_name!r} does not exist")
    blk = Block(dataset_id=ds.id, name=block_name)
    db.add(blk)
    db.flush()
    return blk


def delete_block(db: Session, dataset_name: str, block_name: str) -> bool:
    blk = get_block(db, dataset_name, block_name)
    if blk is None:
        return False
    db.delete(blk)
    db.flush()
    return True
