import logging
import os
import threading
from datetime import datetime, timedelta
from queue import Queue, Full
from typing import Optional

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

BATCH_SIZE = 100


class TargetScoreLogger:

    def __init__(self, target_species: list, output_dir: str,
                 max_file_mb: int = 200, queue_maxsize: int = 2000):
        self.target_species = sorted(target_species)
        self.target_set = set(target_species)
        self.output_dir = output_dir
        self.max_file_bytes = max_file_mb * 1024 * 1024
        self._queue = Queue(maxsize=queue_maxsize)
        self._schema = self._build_schema()
        self._writer: Optional[pq.ParquetWriter] = None
        self._current_file_key: Optional[str] = None
        self._current_path: Optional[str] = None
        self._part = 1
        self._batch: list[dict] = []
        self._thread: Optional[threading.Thread] = None
        self._shutdown = False

    def _build_schema(self) -> pa.Schema:
        fields = [
            pa.field("timestamp", pa.timestamp("us")),
            pa.field("file_name", pa.string()),
            pa.field("window_start", pa.float32()),
            pa.field("window_end", pa.float32()),
        ]
        for species in self.target_species:
            fields.append(pa.field(species, pa.float16()))
        return pa.schema(fields)

    def start(self):
        os.makedirs(self.output_dir, exist_ok=True)
        self._thread = threading.Thread(target=self._writer_loop, daemon=True, name="score-logger")
        self._thread.start()

    def stop(self):
        self._shutdown = True
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=10)
        self._flush_batch()
        self._close_writer()

    def log_scores(self, file_date: datetime, file_name: str,
                   window_start: float, window_end: float,
                   predictions: list):
        if not self.target_set:
            return
        scores_dict = dict(predictions)
        row = {
            "timestamp": file_date + timedelta(seconds=window_start),
            "file_name": file_name,
            "window_start": window_start,
            "window_end": window_end,
        }
        for sp in self.target_species:
            row[sp] = scores_dict.get(sp, 0.0)
        try:
            self._queue.put_nowait(row)
        except Full:
            log.warning("Score logger queue full, dropping scores for %s @ %.1f", file_name, window_start)

    def _writer_loop(self):
        while True:
            row = self._queue.get()
            if row is None:
                self._queue.task_done()
                break
            try:
                self._accumulate(row)
            except Exception:
                log.exception("Error writing score row")
            self._queue.task_done()

    def _file_key(self, ts: datetime) -> str:
        return ts.strftime("%Y-%m-%d_%H")

    def _make_path(self, key: str, part: int) -> str:
        if part == 1:
            return os.path.join(self.output_dir, f"scores_{key}.parquet")
        return os.path.join(self.output_dir, f"scores_{key}_part{part}.parquet")

    def _ensure_writer(self, key: str):
        if key != self._current_file_key:
            self._flush_batch()
            self._close_writer()
            self._current_file_key = key
            self._part = 1
            self._current_path = self._make_path(key, self._part)
            self._writer = pq.ParquetWriter(self._current_path, self._schema, compression="gzip")
        elif self._current_path and os.path.exists(self._current_path) and \
                os.path.getsize(self._current_path) >= self.max_file_bytes:
            self._flush_batch()
            self._close_writer()
            self._part += 1
            self._current_path = self._make_path(key, self._part)
            self._writer = pq.ParquetWriter(self._current_path, self._schema, compression="gzip")

    def _accumulate(self, row: dict):
        key = self._file_key(row["timestamp"])
        self._ensure_writer(key)
        self._batch.append(row)
        if len(self._batch) >= BATCH_SIZE:
            self._flush_batch()

    def _flush_batch(self):
        if not self._batch or self._writer is None:
            return
        arrays = [
            pa.array([r["timestamp"] for r in self._batch], type=pa.timestamp("us")),
            pa.array([r["file_name"] for r in self._batch], type=pa.string()),
            pa.array([r["window_start"] for r in self._batch], type=pa.float32()),
            pa.array([r["window_end"] for r in self._batch], type=pa.float32()),
        ]
        for species in self.target_species:
            arrays.append(pa.array([np.float16(r[species]) for r in self._batch], type=pa.float16()))
        batch = pa.RecordBatch.from_arrays(arrays, schema=self._schema)
        self._writer.write_batch(batch)
        self._batch.clear()

    def _close_writer(self):
        if self._writer is not None:
            self._writer.close()
            self._writer = None
