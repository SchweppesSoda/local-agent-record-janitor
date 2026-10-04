import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent_record_janitor import office_files as office

SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
PRIVATE = b"PRIVATE_TRANSCRIPT_BODY"


class OfficeFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.children = [{"id": "selected-child", "chat_id": "chat-selected", "session_id": SID,
                          "session_memory_path": str(self.root / f"projects/bucket/{SID}/session-memory/summary.md")}]

    def write(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PRIVATE)
        return path

    def freeze(self, **kwargs):
        return office.freeze(self.root, client="qwenwork", role="sdk", children=self.children,
                             all_child_ids=["selected-child", "other-child"], **kwargs)

    def fixture(self):
        selected = [f"projects/bucket/{SID}.jsonl", f"projects/bucket/{SID}/state.json",
            f"projects/bucket/{SID}/session-memory/summary.md", f"projects/bucket/{SID}/subagents/agent-a1.jsonl",
            f"projects/old-bucket/{SID}.jsonl", f"file-history/{SID}/checkpoint",
            f"tmp/bucket/{SID}/cache", f"tmp/bucket/logs/session-{SID}.jsonl",
            f"tmp/bucket/tool-outputs/session-{SID}/result", "tmp/bucket/a1/cache",
            f"tmp/bucket/images/{SID}/paste.png", "tmp/bucket/images/a1/paste.png",
            "tmp/bucket/logs/session-a1.jsonl", "tmp/bucket/tool-outputs/session-a1/result",
            f"logs/sessions/bucket/{SID}/segments/part.jsonl", "shell-outputs/selected-child/result",
            "sandbox-runtime/sessions/selected-child/cache"]
        other = [f"projects/bucket/{OTHER}.jsonl", f"projects/bucket/{OTHER}/state.json",
            "projects/bucket/memory/shared", "plans/generated.md", "workspace/chat-selected/document.txt",
            "shell-outputs/other-child/output", "sandbox-runtime/sessions/other-child/cache", "tmp/bucket/a2/cache"]
        for name in selected + other:
            self.write(name)
        return selected, other

    def test_all_owned_locations_and_subagents_removed_outputs_and_shared_memory_preserved(self):
        selected, others = self.fixture()
        evidence = self.freeze()
        self.assertNotIn(PRIVATE.decode(), json.dumps(evidence))
        self.assertEqual(evidence["agent_owners"], {"a1": SID})
        office.apply(evidence, phase_callback=lambda _: None)
        self.assertEqual(office.remaining(evidence), 0)
        self.assertTrue(all(not (self.root / name).exists() for name in selected))
        self.assertTrue(all((self.root / name).read_bytes() == PRIVATE for name in others))

    def test_new_copy_or_subagent_cache_change_refuses_before_deletion(self):
        self.fixture()
        evidence = self.freeze()
        self.write(f"projects/new-bucket/{SID}.jsonl")
        phases = []
        with self.assertRaisesRegex(office.OfficeDatabaseError, "closure_changed"):
            office.apply(evidence, phase_callback=phases.append)
        self.assertEqual(phases, [])
        self.assertTrue((self.root / f"projects/bucket/{SID}.jsonl").exists())

    def test_partial_failure_retains_frozen_subagent_ids_for_readonly_recovery(self):
        self.fixture()
        evidence = self.freeze()
        original = office.frozen_files.apply_remove
        def fail_on_temp(root, item):
            if item["path"].startswith("tmp/"):
                raise OSError("synthetic interruption")
            original(root, item)
        with patch.object(office.frozen_files, "apply_remove", fail_on_temp):
            with self.assertRaises(OSError):
                office.apply(evidence, phase_callback=lambda _: None)
        self.assertFalse((self.root / f"projects/bucket/{SID}").exists())
        self.assertGreater(office.remaining(evidence), 0)
        self.assertTrue((self.root / "tmp/bucket/a1/cache").exists())

    def test_shared_subagent_and_sanitizer_collision_are_blocked(self):
        self.fixture()
        self.write(f"projects/bucket/{OTHER}/subagents/agent-a1.jsonl")
        with self.assertRaisesRegex(office.OfficeDatabaseError, "subagent_owner_ambiguous"):
            self.freeze()
        with self.assertRaisesRegex(office.OfficeDatabaseError, "cache_owner_ambiguous"):
            office.freeze(self.root, client="qwenwork", role="sdk",
                children=[{"id": "child:one", "chat_id": "chat", "session_id": None}],
                all_child_ids=["child:one", "child_one"])

    def test_case_aliases_do_not_delete_an_unselected_session_cache(self):
        self.write("shell-outputs/child/cache")
        with self.assertRaisesRegex(office.OfficeDatabaseError, "cache_owner_ambiguous"):
            office.freeze(self.root, client="qwenwork", role="sdk",
                children=[{"id": "CHILD", "chat_id": "chat", "session_id": None}], all_child_ids=["CHILD", "child"])
        self.assertTrue((self.root / "shell-outputs/child/cache").exists())
        self.fixture()
        self.write(f"projects/bucket/{OTHER}/subagents/agent-A1.meta.json")
        with self.assertRaisesRegex(office.OfficeDatabaseError, "subagent_owner_ambiguous"):
            self.freeze()

    def test_meta_only_dotted_agent_retains_its_owned_temp_cache_in_the_plan(self):
        self.write(f"projects/bucket/{SID}/subagents/agent-review.v2.meta.json")
        path = self.write("tmp/bucket/images/review.v2/image.png")
        evidence = self.freeze()
        self.assertEqual(evidence["agent_owners"], {"review.v2": SID})
        office.apply(evidence, phase_callback=lambda _: None)
        self.assertFalse(path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive namespace")
    def test_selected_uppercase_uuid_matches_lowercase_physical_directory(self):
        sid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.children[0]["session_id"] = sid.upper()
        self.children[0]["session_memory_path"] = None
        self.write(f"projects/bucket/{sid}/subagents/agent-a1.jsonl")
        selected = self.write("tmp/bucket/a1/cache")
        evidence = self.freeze()
        office.apply(evidence, phase_callback=lambda _: None)
        self.assertFalse(selected.exists())
        self.assertEqual(office.remaining(evidence), 0)

    def test_sensitive_vault_deletes_only_hashed_session_scope_without_reading_credentials(self):
        identity = "a" * 64
        scope = hashlib.sha256(b"selected-child").hexdigest() + ".json"
        selected = self.write("data/sensitive-vault/" + identity + "/" + scope)
        others = [self.write("data/sensitive-vault/master-key-v1.enc"),
                  self.write("data/sensitive-vault/" + identity + "/" + "b" * 64 + ".json")]
        evidence = office.freeze(self.root, client="qwenwork", role="profile", children=self.children,
                                 all_child_ids=["selected-child", "other-child"])
        self.assertNotIn(PRIVATE.decode(), json.dumps(evidence))
        office.apply(evidence, phase_callback=lambda _: None)
        self.assertFalse(selected.exists())
        self.assertTrue(all(path.read_bytes() == PRIVATE for path in others))


if __name__ == "__main__":
    unittest.main()
