import json
from pathlib import Path
import subprocess
import unittest

from local_agent_record_janitor.office_leveldb import _node, _environment


class PaseoPhysicalTests(unittest.TestCase):
    def test_draft_and_workspace_preservation_vectors(self):
        fixture = Path(__file__).parent / 'fixtures/paseo_ui_vectors.cjs'
        result = subprocess.run([str(_node()), str(fixture)], capture_output=True,
                                timeout=30, env=_environment())
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        value = json.loads(result.stdout)
        self.assertEqual(value['passed'], value['total'], value)
        self.assertEqual(value['total'], 21)

    def test_independent_physical_corruption_and_version_vectors(self):
        fixture = Path(__file__).parent / 'fixtures/paseo_indexeddb_physical.cjs'
        result = subprocess.run([str(_node()), str(fixture)], capture_output=True,
                                timeout=30, env=_environment())
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        value = json.loads(result.stdout)
        self.assertTrue(value['passed'], value)
        self.assertEqual(len(value['results']), 11)


if __name__ == '__main__':
    unittest.main()
