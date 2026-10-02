from __future__ import annotations

import tempfile
import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_agent_record_janitor.adapters import CindyAdapter
from local_agent_record_janitor.client_inventory import (
    ClientTarget, _bind_native_targets, _retarget_target,
    build_client_engine_contexts, build_client_inventory,
)
from local_agent_record_janitor.client_inventory import build_native_client_inventory, project_client_evidence
from local_agent_record_janitor.client_contracts import (
    ClientContractError, ClientDescriptor, ClientReference, ReferenceKind, ReferenceLifecycle,
    ReferenceSnapshot, SourceFailure, describe_adapter,
)
from local_agent_record_janitor.pi_sessions import build_pi_session_catalog
from local_agent_record_janitor.pi_sessions import PiMultiRootCatalog
from local_agent_record_janitor.claude_sessions import build_claude_session_catalog
from local_agent_record_janitor.inventory import FrontendSessionRecord
from local_agent_record_janitor.record_identity import EngineCapability, RecordClassification, RecordKey, StoreKey
from tests.test_client_inventory import _write_pi_session, _write_claude_session
from tests.test_cindy_references import create_database


class _LimitedCindy(CindyAdapter):
    def capability_limit_for(self, engine: str) -> EngineCapability:
        return EngineCapability("cindy", engine, reason="Read-only profile")


class ExistingContractGapTests(unittest.TestCase):
    def test_incomplete_native_binding_does_not_restrict_an_independent_frontend_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [_write_pi_session(root / name, name) for name in ("a", "c")]
            catalogs = [build_pi_session_catalog(agent_dir=root / name, session_root=root / name / "sessions")
                        for name in ("a", "c")]
            capability = EngineCapability("cindy", "pi", native_delete=True, frontend_reference_delete=True)
            incomplete = ClientReference("cindy", root / "a" / "metadata.json", "a-ui", "a", "pi", "pi", "a-binding",
                native_record=RecordKey(StoreKey("pi", catalogs[0].session_root, kind="session_root"),
                                        "a", kind="session", path=paths[0]), evidence_complete=False)
            complete = ClientReference("cindy", root / "b" / "metadata.json", "b-ui", None, "pi", "pi", "b-binding",
                                       evidence_complete=True)
            frontend = ClientTarget("cindy", "pi", None, None, None, ("cindy:b-ui",),
                RecordClassification.ORPHAN_FRONTEND, capability,
                action_ids=("remove_frontend_reference:b",), frontend_binding_keys=("b-binding",),
                references=(complete,))
            records = [record for catalog in catalogs for record in catalog.records]
            for order in (records, list(reversed(records))):
                targets = _bind_native_targets("cindy", "pi", (frontend,), (),
                    SimpleNamespace(records=order), capability, reference_evidence=(incomplete, complete))
                bound = next(t for t in targets if t.record_id == "a")
                self.assertIn("reference_inventory_incomplete", bound.blocker_codes)
                self.assertFalse(bound.action_ids)
                residual = next(t for t in targets if t.record_key is None)
                self.assertTrue(residual.capability.frontend_reference_delete)
                self.assertNotIn("reference_inventory_incomplete", residual.blocker_codes)
                self.assertIn("remove_frontend_reference:b", residual.action_ids)

    def test_readonly_retarget_removes_compatibility_action_ids_and_cleanup_eligibility(self) -> None:
        target = ClientTarget("cindy", "codex", None, None, "native-id", ("cindy:ui",),
            RecordClassification.ORPHAN_FRONTEND,
            EngineCapability("cindy", "codex", frontend_session_delete=True, frontend_reference_delete=True),
            action_ids=("cindy:ui", "delete_frontend_session", "remove_frontend_reference:ui"))
        restricted = _retarget_target(target, EngineCapability("cindy", "codex"))
        self.assertFalse(restricted.action_ids)
        self.assertFalse(restricted.to_dict()["cleanup_eligible"])

    def test_inherited_descriptor_checks_host_before_constructing_a_local_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = CindyAdapter(database=root / "metadata.db", codex_home=root / "codex-home", cindy_root=root)
            adapter.host = "ssh:synthetic-host"
            with patch("local_agent_record_janitor.adapters.base.StoreKey", side_effect=AssertionError("remote path")):
                with self.assertRaises(ClientContractError):
                    adapter.describe_client()

    def test_typed_native_binding_is_exact_and_does_not_duplicate_frontend_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [_write_pi_session(root / name, "same-id") for name in ("one", "two")]
            catalogs = tuple(build_pi_session_catalog(agent_dir=root / name, session_root=root / name / "sessions")
                             for name in ("one", "two"))
            stores = tuple(StoreKey("pi", c.session_root, kind="session_root") for c in catalogs)
            source = root / "metadata.json"
            descriptor = ClientDescriptor("cindy", profile_root=root, sources=(source,), native_stores=stores,
                inventory_engines=("pi",), capability_limits=(EngineCapability("cindy", "pi"),))
            reference = ClientReference("cindy", source, "ui", "same-id", "pi", "pi", "binding-one",
                native_record=RecordKey(stores[0], "same-id", kind="session", path=paths[0]),
                kind=ReferenceKind.CURRENT, lifecycle=ReferenceLifecycle.LIVE, evidence_complete=True)

            class Reader:
                def describe_client(self):
                    return descriptor

                def snapshot_references(self, *, refresh=False):
                    return ReferenceSnapshot(descriptor, (reference,))

                def native_catalog_for(self, engine):
                    return PiMultiRootCatalog(catalogs=catalogs)

            context = build_client_engine_contexts((Reader(),), client="cindy", engines=("pi",))[0]
            self.assertEqual(len(context.targets), 2)
            by_path = {t.record_key.path: t for t in context.targets}
            self.assertEqual(by_path[paths[0]].classification, RecordClassification.HEALTHY)
            self.assertEqual(by_path[paths[0]].frontend_binding_keys, ("binding-one",))
            self.assertEqual(by_path[paths[1]].classification, RecordClassification.ORPHAN_NATIVE)
            self.assertFalse(by_path[paths[1]].frontend_binding_keys)
            self.assertTrue(all(not t.action_ids for t in context.targets))

    def test_evidence_projection_indexes_paths_once_for_a_large_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(100):
                _write_pi_session(root, f"session-{index}")
            catalog = build_pi_session_catalog(agent_dir=root, session_root=root / "sessions")
            inventory, _ = build_native_client_inventory(client="pi", engine="pi", catalog=catalog)
            from local_agent_record_janitor.record_identity import canonical_path
            with patch("local_agent_record_janitor.client_inventory.canonical_path", wraps=canonical_path) as probe:
                projected = project_client_evidence(inventory, {"pi": catalog})
            self.assertEqual(len(projected.targets), 100)
            self.assertLessEqual(probe.call_count, 102)

    def test_descriptor_only_reader_preserves_unqualified_and_remote_restore_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "metadata.json"
            descriptor = ClientDescriptor("cindy", profile_root=root, sources=(source,),
                inventory_engines=("codex",), capability_limits=(EngineCapability("cindy", "codex"),))
            reference = ClientReference("cindy", source, "pane", "native-id", "codex", "codex", "restore-1",
                kind=ReferenceKind.RESTORE, lifecycle=ReferenceLifecycle.RESTORABLE,
                host="ssh:synthetic-host", path_namespace="posix", opaque_native_locator="/opaque/remote/root")

            class Reader:
                def describe_client(self):
                    return descriptor

                def snapshot_references(self, *, refresh=False):
                    return ReferenceSnapshot(descriptor, (reference,), (SourceFailure(
                        "snapshot", "Unsupported secondary snapshot", profile_root=root),))

                def native_catalog_for(self, engine):
                    self.fail("Unqualified references cannot trigger native discovery")

            reader = Reader()
            reader.fail = self.fail
            inventory = build_client_inventory((reader,), client="cindy")
            self.assertEqual(inventory.frontend_sessions, ())
            self.assertEqual(inventory.references, (reference,))
            self.assertEqual(len(inventory.errors), 1)
            self.assertFalse(inventory.capabilities["codex"].native_delete)
            context = build_client_engine_contexts((reader,), client="cindy", inventory=inventory)[0]
            self.assertEqual(context.targets[0].classification, RecordClassification.UNVERIFIED)
            self.assertFalse(context.targets[0].action_ids)
            self.assertEqual(context.targets[0].references[0].opaque_native_locator, "/opaque/remote/root")

    def test_local_host_gate_runs_before_legacy_store_path_construction(self) -> None:
        class RemoteReader:
            name = "cindy"
            host = "ssh:synthetic-host"
            path_namespace = "posix"
            codex_home = Path("/opaque/remote/root")

        with patch("local_agent_record_janitor.client_contracts.StoreKey", side_effect=AssertionError("must not interpret remote paths")):
            with self.assertRaises(ClientContractError):
                describe_adapter(RemoteReader())

    def test_current_and_agent_switch_project_as_distinct_typed_references_without_changing_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "codex-home"
            home.mkdir()
            database = root / "cindy.db"
            create_database(database, [("ui", "current", "deleted", "pi")], [
                ("boundary", "ui", {"fromAgentKind": "pi", "fromSdkSessionId": "previous"}, 1, None),
            ])
            adapter = CindyAdapter(database=database, codex_home=home, cindy_root=root)
            snapshot = adapter.snapshot_sessions()
            before = [r.approval_payload() for r in snapshot.records]
            typed = adapter.snapshot_references()
            self.assertEqual({r.kind for r in typed.references}, {ReferenceKind.CURRENT, ReferenceKind.HISTORY})
            self.assertEqual({r.lifecycle for r in typed.references}, {ReferenceLifecycle.DELETED})
            self.assertEqual(len({r.binding_key for r in typed.references}), 2)
            historical = next(r for r in typed.references if r.kind == ReferenceKind.HISTORY)
            self.assertEqual(historical.source_locator, "boundary")
            self.assertEqual([r.approval_payload() for r in snapshot.records], before)
            self.assertEqual(adapter.snapshot_sessions().fingerprint, snapshot.fingerprint)

    def test_native_pi_branch_and_claude_manifest_keep_independent_relation_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pi_root = root / "pi"
            parent = _write_pi_session(pi_root, "parent")
            child = _write_pi_session(pi_root, "child")
            lines = child.read_text(encoding="utf-8").splitlines()
            header = json.loads(lines[0])
            header["parentSession"] = str(parent)
            child.write_text(json.dumps(header) + "\n" + "\n".join(lines[1:]), encoding="utf-8")
            pi_catalog = build_pi_session_catalog(agent_dir=pi_root, session_root=pi_root / "sessions")
            pi_inventory, _ = build_native_client_inventory(client="pi", engine="pi", catalog=pi_catalog)
            relation = next(t for t in pi_inventory.targets if t.record_id == "child").relations[0]
            self.assertEqual(relation.kind.value, "pi_branch_source")
            self.assertEqual(relation.deletion_semantics, "independent")
            self.assertIsNone(relation.related_record)

            sid = "11111111-1111-4111-8111-111111111111"
            transcript = _write_claude_session(root / "claude", sid)
            subagent = transcript.parent / sid / "subagents" / "agent-child.jsonl"
            subagent.parent.mkdir(parents=True)
            subagent.write_text(json.dumps({"sessionId": sid, "parentUuid": "message-parent"}) + "\n", encoding="utf-8")
            claude_catalog = build_claude_session_catalog(config_dir=root / "claude")
            inventory, _ = build_native_client_inventory(client="claude", engine="claude", catalog=claude_catalog)
            self.assertEqual(len(inventory.targets), 1)
            self.assertTrue(inventory.targets[0].relations)
            self.assertEqual({r.kind.value for r in inventory.targets[0].relations}, {"claude_manifest_member"})
            self.assertEqual({r.deletion_semantics for r in inventory.targets[0].relations}, {"manifest_member"})

    def test_frontend_reader_without_codex_home_is_not_silently_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = FrontendSessionRecord("cindy", "ui", "pi-id", root / "metadata.db",
                                        root / "known-codex-home", backend="pi")

            class Reader:
                name = "cindy"
                database = row.database

                def list_sessions(self):
                    return [row]

            inventory = build_client_inventory((Reader(),), client="cindy", engines=("pi",))
            self.assertEqual(len(inventory.frontend_sessions), 1)
            self.assertEqual(len(inventory.targets), 1)

    def test_readonly_profile_limit_survives_without_disabling_independent_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapters = []
            for name, cls in (("writable", CindyAdapter), ("readonly", _LimitedCindy)):
                profile = root / name
                home = profile / "codex-home"
                home.mkdir(parents=True)
                database = profile / "cindy.db"
                create_database(database, [("ui", "same-id", "deleted", "pi")])
                _write_pi_session(profile / "pi-agent-home", "same-id")
                adapters.append(cls(database=database, codex_home=home, cindy_root=profile))
            for order in (adapters, tuple(reversed(adapters))):
                contexts = build_client_engine_contexts(order, client="cindy", engines=("pi",))
                by_root = {target.record_key.store.path.parent.parent.name: target
                           for context in contexts for target in context.targets
                           if target.record_key is not None}
                self.assertTrue(by_root["writable"].capability.native_delete)
                self.assertIn("delete_pi_session", by_root["writable"].action_ids)
                self.assertFalse(by_root["readonly"].capability.native_delete)
                self.assertFalse(by_root["readonly"].action_ids)


if __name__ == "__main__":
    unittest.main()
