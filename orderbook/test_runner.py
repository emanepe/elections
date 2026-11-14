"""The operator command must distinguish unavailable analysis from a broken analysis."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run


class RunnerTests(unittest.TestCase):
    def exercise(self, diagnostic_error):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def capture(command):
                session = Path(command[command.index('--output')+1])
                session.mkdir()
                (session/'status.json').write_text(json.dumps(dict(state='complete')))
                return 0

            def process(command, **kwargs):
                if command[1].endswith('export_orderbook_csv.py'):
                    csv_dir = Path(command[command.index('--output-dir')+1])
                    csv_dir.mkdir()
                    (csv_dir/'polymarket_election_quotes_20261114.csv').write_text('column\nvalue\n')
                    return subprocess.CompletedProcess(command, 0)
                return subprocess.CompletedProcess(command, 1, stdout='', stderr=diagnostic_error)

            with patch.object(run, 'require_node', return_value=('node','v24.18.0')), \
                    patch.object(run, 'capture_wait', side_effect=capture), \
                    patch.object(run.subprocess, 'run', side_effect=process), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = run.main(['--market','test-market','--output',str(root)])
            summary = json.loads(next(root.glob('*/run_summary.json')).read_text())
            return code, summary

    def test_analysis_exception_is_not_reported_as_success(self):
        code, result = self.exercise('Traceback: corrupted input\n')
        self.assertNotEqual(code, 0)
        self.assertEqual(result['analysis_status'], 'failed')
        self.assertFalse(result['successful'])

    def test_no_usable_signal_is_distinct_from_processing_failure(self):
        code, result = self.exercise('No usable quote windows found.\n')
        self.assertEqual(code, 0)
        self.assertEqual(result['analysis_status'], 'no_usable_windows')
        self.assertTrue(result['successful'])


if __name__ == '__main__':
    unittest.main()
