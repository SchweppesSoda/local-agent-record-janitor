from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from local_agent_record_janitor.orca_configuration import MAX_CONFIG_BYTES, package_manifest_paths, validate_configuration


class OrcaConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.config = self.root / "config.toml"

    def check(self, content):
        self.config.write_bytes(content)
        validate_configuration(self.config, {"sha256": hashlib.sha256(content).hexdigest()})

    def test_model_preferences_and_project_trust_are_accepted_without_opening_project(self):
        outside = self.root / "never-created-project"
        content = '\n'.join(('model="gpt-test"', 'model_provider="openai"',
            'model_reasoning_effort="high"', 'approval_policy="never"', 'sandbox_mode="workspace-write"',
            'cli_auth_credentials_store="keyring"', 'check_for_update_on_startup=false',
            '[projects.' + json.dumps(str(outside)) + ']', 'trust_level="trusted"'))
        self.check(content.encode())
        self.assertFalse(outside.exists())

    def test_unknown_routing_hooks_credentials_and_malformed_toml_never_leak_values(self):
        cases = ('sqlite_home="PRIVATE_SENTINEL"', 'log_dir="PRIVATE_SENTINEL"',
            '[profiles.test]\nmodel="PRIVATE_SENTINEL"', '[mcp_servers.test]\ncommand="PRIVATE_SENTINEL"',
            '[model_providers.other]\nbase_url="PRIVATE_SENTINEL"', 'model=42',
            'approval_policy={reject={mcp_elicitations=true}}', 'check_for_update_on_startup="false"',
            'model="PRIVATE_SENTINEL"\nmodel="duplicate"', 'model="unterminated PRIVATE_SENTINEL',
            'model_reasoning_effort="future"', 'projects={relative={trust_level="trusted"}}',
            '# ' + 'x' * MAX_CONFIG_BYTES)
        for value in cases:
            with self.subTest(value=value[:40]), self.assertRaisesRegex(ValueError, '^orca_storage_configuration_unverified$'):
                self.check(value.encode())

    def test_configuration_replacement_after_digest_is_rejected(self):
        self.config.write_bytes(b'model="new"')
        with self.assertRaisesRegex(ValueError, '^orca_storage_configuration_unverified$'):
            validate_configuration(self.config, {"sha256": hashlib.sha256(b'model="old"').hexdigest()})

    def test_package_context_checks_parent_manifest_without_reading_untrusted_values(self):
        binary = self.root / "bin" / "codex.exe"
        self.assertEqual(package_manifest_paths(binary), (binary.parent / "codex-package.json", self.root / "codex-package.json"))
        with self.assertRaisesRegex(ValueError, "orca_package_context_unverified"):
            package_manifest_paths(self.root / "packages" / "standalone" / "releases" / "version" / "codex.exe")
