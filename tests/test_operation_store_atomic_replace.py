import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from local_agent_record_janitor import operation_store
from local_agent_record_janitor.operation_store import OperationStoreError


class AtomicWriteJsonReplaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve(strict=True)
        self.path = self.directory / "state.json"

    @staticmethod
    def _windows_error(code: int) -> OSError:
        error = PermissionError(code, "transient replace failure")
        error.winerror = code
        return error

    @staticmethod
    def _os_proxy(name, replace):
        proxy = Mock(wraps=operation_store.os)
        proxy.name = name
        proxy.replace = Mock(side_effect=replace)
        return proxy

    @staticmethod
    def _time_proxy(*, monotonic=None):
        proxy = Mock(wraps=operation_store.time)
        proxy.sleep = Mock(return_value=None)
        if monotonic is not None:
            proxy.monotonic = Mock(side_effect=monotonic)
        return proxy

    def test_transient_windows_lock_retries_same_temp_file(self):
        before = b'{"old":true}\n'
        self.path.write_bytes(before)
        real_replace = operation_store.os.replace
        calls: list[tuple[Path, Path]] = []

        def replace(source, destination):
            calls.append((Path(source), Path(destination)))
            if len(calls) <= 2:
                raise self._windows_error(32)
            return real_replace(source, destination)

        os_proxy = self._os_proxy("nt", replace)
        time_proxy = self._time_proxy()
        with patch.object(operation_store, "os", os_proxy), patch.object(
            operation_store, "time", time_proxy
        ):
            operation_store.atomic_write_json(self.path, {"new": True})

        self.assertEqual(self.path.read_bytes(), b'{"new":true}\n')
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({source for source, _ in calls}), 1)
        self.assertTrue(all(destination == self.path for _, destination in calls))
        self.assertFalse(calls[0][0].exists())

    def test_exhausted_windows_retries_keep_old_file_and_encode_once(self):
        before = b'{"old":true}\n'
        self.path.write_bytes(before)
        calls: list[tuple[Path, Path]] = []

        def always_fail(source, destination):
            calls.append((Path(source), Path(destination)))
            raise self._windows_error(5)

        os_proxy = self._os_proxy("nt", always_fail)
        time_proxy = self._time_proxy()
        with patch.object(
            operation_store, "canonical_json_bytes", wraps=operation_store.canonical_json_bytes
        ) as encode, patch.object(operation_store, "os", os_proxy), patch.object(
            operation_store, "time", time_proxy
        ):
            with self.assertRaises(OperationStoreError):
                operation_store.atomic_write_json(self.path, {"new": True})

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(calls), operation_store._ATOMIC_REPLACE_MAX_ATTEMPTS)
        self.assertEqual(len({source for source, _ in calls}), 1)
        self.assertEqual(encode.call_count, 1)
        self.assertFalse(list(self.directory.glob(".state.json.agent-*.tmp")))

    def test_unrelated_replace_error_is_not_retried(self):
        before = b'{"old":true}\n'
        self.path.write_bytes(before)
        calls = []

        def fail(source, destination):
            calls.append((Path(source), Path(destination)))
            error = OSError(87, "unrelated replace failure")
            error.winerror = 87
            raise error

        os_proxy = self._os_proxy("nt", fail)
        time_proxy = self._time_proxy()
        with patch.object(operation_store, "os", os_proxy), patch.object(
            operation_store, "time", time_proxy
        ):
            with self.assertRaises(OperationStoreError):
                operation_store.atomic_write_json(self.path, {"new": True})

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(calls), 1)
        time_proxy.sleep.assert_not_called()
        self.assertFalse(list(self.directory.glob(".state.json.agent-*.tmp")))

    def test_windows_lock_error_is_not_retried_on_non_windows(self):
        source = self.directory / "temporary.json"
        source.write_bytes(b'{"new":true}\n')
        before = b'{"old":true}\n'
        self.path.write_bytes(before)
        calls = []

        def fail(source_path, destination):
            calls.append((Path(source_path), Path(destination)))
            raise self._windows_error(33)

        os_proxy = self._os_proxy("posix", fail)
        time_proxy = self._time_proxy()
        with patch.object(operation_store, "os", os_proxy), patch.object(
            operation_store, "time", time_proxy
        ):
            with self.assertRaises(OSError):
                operation_store._atomic_replace_with_retry(source, self.path)

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(calls), 1)
        time_proxy.sleep.assert_not_called()
        self.assertTrue(source.exists())

    def test_windows_lock_error_stops_when_retry_deadline_expires(self):
        source = self.directory / "temporary.json"
        source.write_bytes(b'{"new":true}\n')
        self.path.write_bytes(b'{"old":true}\n')
        calls = []

        def fail(source_path, destination):
            calls.append((Path(source_path), Path(destination)))
            raise self._windows_error(5)

        os_proxy = self._os_proxy("nt", fail)
        time_proxy = self._time_proxy(monotonic=[100.0, 100.2])
        with patch.object(operation_store, "os", os_proxy), patch.object(
            operation_store, "time", time_proxy
        ):
            with self.assertRaises(OSError):
                operation_store._atomic_replace_with_retry(source, self.path)

        self.assertEqual(len(calls), 1)
        time_proxy.sleep.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'{"old":true}\n')
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
