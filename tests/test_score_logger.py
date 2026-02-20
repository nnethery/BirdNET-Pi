import os
import shutil
import tempfile
import unittest
from datetime import datetime

import pyarrow.parquet as pq

from scripts.utils.score_logger import TargetScoreLogger


class TestTargetScoreLogger(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.species = ['Pica pica', 'Turdus merula']

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_predictions(self, target_scores=None):
        if target_scores is None:
            target_scores = {'Pica pica': 0.95, 'Turdus merula': 0.42}
        return [(k, v) for k, v in target_scores.items()] + [('Corvus corone', 0.1)]

    def test_schema_columns(self):
        logger = TargetScoreLogger(self.species, self.tmpdir)
        schema = logger._build_schema()
        names = schema.names
        self.assertEqual(names[:4], ['timestamp', 'file_name', 'window_start', 'window_end'])
        self.assertEqual(names[4:], ['Pica pica', 'Turdus merula'])

    def test_schema_sorted_species(self):
        logger = TargetScoreLogger(['Zebra finch', 'Anas duck'], self.tmpdir)
        self.assertEqual(logger.target_species, ['Anas duck', 'Zebra finch'])

    def test_write_and_read_roundtrip(self):
        logger = TargetScoreLogger(self.species, self.tmpdir)
        logger.start()

        ts = datetime(2026, 2, 19, 14, 0, 0)
        logger.log_scores(ts, 'test.wav', 0.0, 3.0, self._make_predictions())
        logger.log_scores(ts, 'test.wav', 3.0, 6.0, self._make_predictions({'Pica pica': 0.88, 'Turdus merula': 0.01}))
        logger.stop()

        files = [f for f in os.listdir(self.tmpdir) if f.endswith('.parquet')]
        self.assertEqual(len(files), 1)

        table = pq.read_table(os.path.join(self.tmpdir, files[0]))
        self.assertEqual(table.num_rows, 2)
        self.assertIn('Pica pica', table.column_names)
        self.assertIn('Turdus merula', table.column_names)

        pp_scores = table.column('Pica pica').to_pylist()
        self.assertAlmostEqual(float(pp_scores[0]), 0.95, places=2)
        self.assertAlmostEqual(float(pp_scores[1]), 0.88, places=2)

    def test_float16_quantization(self):
        logger = TargetScoreLogger(self.species, self.tmpdir)
        logger.start()

        ts = datetime(2026, 2, 19, 14, 0, 0)
        logger.log_scores(ts, 'test.wav', 0.0, 3.0, self._make_predictions())
        logger.stop()

        table = pq.read_table(os.path.join(self.tmpdir, os.listdir(self.tmpdir)[0]))
        schema = table.schema
        import pyarrow as pa
        self.assertEqual(schema.field('Pica pica').type, pa.float16())

    def test_gzip_compression(self):
        logger = TargetScoreLogger(self.species, self.tmpdir)
        logger.start()

        ts = datetime(2026, 2, 19, 14, 0, 0)
        logger.log_scores(ts, 'test.wav', 0.0, 3.0, self._make_predictions())
        logger.stop()

        path = os.path.join(self.tmpdir, os.listdir(self.tmpdir)[0])
        meta = pq.read_metadata(path)
        codec = meta.row_group(0).column(0).compression
        self.assertEqual(codec, 'GZIP')

    def test_hourly_file_rotation(self):
        logger = TargetScoreLogger(self.species, self.tmpdir)
        logger.start()

        ts_14 = datetime(2026, 2, 19, 14, 30, 0)
        ts_15 = datetime(2026, 2, 19, 15, 5, 0)
        logger.log_scores(ts_14, 'a.wav', 0.0, 3.0, self._make_predictions())
        logger.log_scores(ts_15, 'b.wav', 0.0, 3.0, self._make_predictions())
        logger.stop()

        files = sorted(os.listdir(self.tmpdir))
        self.assertEqual(len(files), 2)
        self.assertIn('2026-02-19_14', files[0])
        self.assertIn('2026-02-19_15', files[1])

    def test_max_file_size_rotation(self):
        logger = TargetScoreLogger(self.species, self.tmpdir, max_file_mb=0)
        logger.max_file_bytes = 1  # force rotation after first write
        logger.start()

        ts = datetime(2026, 2, 19, 14, 0, 0)
        logger.log_scores(ts, 'a.wav', 0.0, 3.0, self._make_predictions())
        logger.log_scores(ts, 'a.wav', 3.0, 6.0, self._make_predictions())
        logger.log_scores(ts, 'a.wav', 6.0, 9.0, self._make_predictions())
        logger.stop()

        files = sorted(os.listdir(self.tmpdir))
        self.assertGreater(len(files), 1)
        self.assertTrue(any('part' in f for f in files))

    def test_missing_species_defaults_to_zero(self):
        logger = TargetScoreLogger(self.species, self.tmpdir)
        logger.start()

        ts = datetime(2026, 2, 19, 14, 0, 0)
        predictions = [('Corvus corone', 0.9)]
        logger.log_scores(ts, 'test.wav', 0.0, 3.0, predictions)
        logger.stop()

        table = pq.read_table(os.path.join(self.tmpdir, os.listdir(self.tmpdir)[0]))
        pp = float(table.column('Pica pica').to_pylist()[0])
        tm = float(table.column('Turdus merula').to_pylist()[0])
        self.assertEqual(pp, 0.0)
        self.assertEqual(tm, 0.0)

    def test_empty_species_is_noop(self):
        logger = TargetScoreLogger([], self.tmpdir)
        logger.start()

        ts = datetime(2026, 2, 19, 14, 0, 0)
        logger.log_scores(ts, 'test.wav', 0.0, 3.0, [('Pica pica', 0.9)])
        logger.stop()

        files = os.listdir(self.tmpdir)
        self.assertEqual(len(files), 0)

    def test_queue_overflow_drops_without_blocking(self):
        logger = TargetScoreLogger(self.species, self.tmpdir, queue_maxsize=1)
        # don't start the writer thread so the queue stays full
        ts = datetime(2026, 2, 19, 14, 0, 0)
        logger.log_scores(ts, 'a.wav', 0.0, 3.0, self._make_predictions())
        # second call should drop, not block
        logger.log_scores(ts, 'b.wav', 0.0, 3.0, self._make_predictions())

    def test_session_folder_created(self):
        session_dir = os.path.join(self.tmpdir, 'session_2026-02-19_14-00-00')
        logger = TargetScoreLogger(self.species, session_dir)
        logger.start()

        ts = datetime(2026, 2, 19, 14, 0, 0)
        logger.log_scores(ts, 'test.wav', 0.0, 3.0, self._make_predictions())
        logger.stop()

        self.assertTrue(os.path.isdir(session_dir))
        self.assertGreater(len(os.listdir(session_dir)), 0)


if __name__ == '__main__':
    unittest.main()
