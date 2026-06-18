from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from cheesemonger.schemas.common import ChunkDim, DatatypeSpec, Dimension
from cheesemonger.schemas.dataset import (
    BlockInfo,
    DatasetCreated,
    DatasetDetail,
    DatasetIn,
    DatasetListOut,
    DatasetSummary,
    DimensionInfo,
)

_LABEL_TRUNCATION_THRESHOLD = 100


class DatasetService:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _dataset_dir(self, name: str) -> Path:
        return self.data_dir / name

    def _schema_path(self, name: str) -> Path:
        return self._dataset_dir(name) / "schema.json"

    def _blocks_dir(self, name: str) -> Path:
        return self._dataset_dir(name) / "blocks"

    def exists(self, name: str) -> bool:
        return self._schema_path(name).is_file()

    def create(self, dataset_in: DatasetIn) -> DatasetCreated:
        ds_dir = self._dataset_dir(dataset_in.name)
        ds_dir.mkdir(parents=True, exist_ok=True)
        self._blocks_dir(dataset_in.name).mkdir(exist_ok=True)

        schema = dataset_in.model_dump()
        self._schema_path(dataset_in.name).write_text(
            json.dumps(schema, indent=2, default=str)
        )

        return DatasetCreated(
            name=dataset_in.name,
            last_dimension=dataset_in.last_dimension,
            dimensions=len(dataset_in.dimensions),
            datatypes=len(dataset_in.datatypes),
            chunk_shape=dataset_in.chunk_shape,
        )

    def list_datasets(self) -> DatasetListOut:
        summaries: list[DatasetSummary] = []
        if not self.data_dir.exists():
            return DatasetListOut(datasets=[])

        for child in sorted(self.data_dir.iterdir()):
            schema_path = child / "schema.json"
            if not schema_path.is_file():
                continue
            schema = json.loads(schema_path.read_text())
            blocks_dir = child / "blocks"
            block_count = sum(1 for b in blocks_dir.iterdir() if b.is_dir()) if blocks_dir.exists() else 0
            summaries.append(
                DatasetSummary(
                    name=schema["name"],
                    blocks=block_count,
                    datatypes=len(schema["datatypes"]),
                )
            )
        return DatasetListOut(datasets=summaries)

    def get_detail(self, name: str) -> DatasetDetail | None:
        if not self.exists(name):
            return None

        schema = json.loads(self._schema_path(name).read_text())

        dims: list[DimensionInfo] = []
        for d in schema["dimensions"]:
            labels = d["labels"]
            size = len(labels)
            if size > _LABEL_TRUNCATION_THRESHOLD:
                dims.append(DimensionInfo(
                    name=d["name"],
                    size=size,
                    labels_truncated=True,
                    labels_sample=labels[:5],
                ))
            else:
                dims.append(DimensionInfo(name=d["name"], size=size, labels=labels))

        blocks: list[BlockInfo] = []
        blocks_dir = self._blocks_dir(name)
        if blocks_dir.exists():
            for b in sorted(blocks_dir.iterdir()):
                if b.is_dir():
                    stat = b.stat()
                    loaded_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
                    blocks.append(BlockInfo(name=b.name, loaded_at=loaded_at))

        datatypes = [DatatypeSpec(**dt) for dt in schema["datatypes"]]
        chunk_shape = [ChunkDim(**c) for c in schema.get("chunk_shape", [])]

        return DatasetDetail(
            name=name,
            last_dimension=schema["last_dimension"],
            dimensions=dims,
            blocks=blocks,
            datatypes=datatypes,
            chunk_shape=chunk_shape,
        )

    def get_schema(self, name: str) -> dict | None:
        if not self.exists(name):
            return None
        return json.loads(self._schema_path(name).read_text())

    def list_block_names(self, name: str) -> list[str]:
        blocks_dir = self._blocks_dir(name)
        if not blocks_dir.exists():
            return []
        return sorted(b.name for b in blocks_dir.iterdir() if b.is_dir())

    def delete_dataset(self, name: str) -> bool:
        ds_dir = self._dataset_dir(name)
        if not ds_dir.exists():
            return False
        shutil.rmtree(ds_dir)
        return True

    def block_exists(self, dataset: str, block: str) -> bool:
        return (self._blocks_dir(dataset) / block).is_dir()

    def delete_block(self, dataset: str, block: str) -> bool:
        block_dir = self._blocks_dir(dataset) / block
        if not block_dir.exists():
            return False
        shutil.rmtree(block_dir)
        return True

    def get_block_zarr_path(self, dataset: str, block: str) -> Path:
        return self._blocks_dir(dataset) / block

    def get_dimension_labels(self, name: str, dim_name: str) -> list | None:
        schema = self.get_schema(name)
        if schema is None:
            return None
        for d in schema["dimensions"]:
            if d["name"] == dim_name:
                return d["labels"]
        return None
