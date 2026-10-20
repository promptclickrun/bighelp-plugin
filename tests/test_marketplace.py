from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from loopdy_plugin.marketplace import (
    MarketplaceGatewayClient,
    HermesHubCLI,
    MarketplaceHTTPResponse,
    MarketplaceInstallError,
    MarketplaceSkillInstaller,
    MARKETPLACE_SKILL_CAPABILITY,
    parse_skill_package,
)
from loopdy_plugin.store import BighelpStore


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _skill_package(*, name: str = "focus-helper") -> bytes:
    value = {
        "schemaVersion": 1,
        "kind": "skill",
        "content": {
            "name": name,
            "skillMd": (
                "---\n"
                f"name: {name}\n"
                "description: Prepare a short, reviewable focus plan.\n"
                "---\n\n"
                "# Focus Helper\n\n"
                "Prepare a plan and wait for user approval before taking actions.\n"
            ),
            "files": {
                "references/usage.md": base64.b64encode(
                    b"# Usage\n\nUse only after explicit user approval.\n"
                ).decode("ascii"),
                "templates/check.txt": base64.b64encode(
                    b"print('this file is installed, never executed')\n"
                ).decode("ascii"),
            },
        },
    }
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class _ReleaseClient:
    def __init__(self, package: bytes):
        self.package = package
        self.requests: list[dict] = []

    async def redeem_and_fetch_skill(self, payload: dict) -> bytes:
        self.requests.append(payload)
        return self.package


class _HubClient:
    def __init__(self, *, present: bool = False, install_error: str | None = None):
        self.present = present
        self.install_error = install_error
        self.calls: list[tuple] = []

    async def validate_profile(self, agent_id: str) -> None:
        self.calls.append(("profile", agent_id))

    async def contains(self, agent_id: str, skill_name: str, *, source: str = "hub") -> bool:
        self.calls.append(("contains", agent_id, skill_name, source))
        return self.present

    async def install(self, agent_id: str, identifier: str, skill_name: str) -> None:
        self.calls.append(("install", agent_id, identifier, skill_name))
        if self.install_error:
            raise MarketplaceInstallError(self.install_error)
        self.present = True


def _skills_list_output(
    rows: list[tuple[str, str, str, str, str]],
    *,
    hub: int = 0,
    builtin: int = 0,
    local: int = 0,
) -> str:
    lines = [
        "┏━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┓",
        "┃ Name   ┃ Category ┃ Source ┃ Trust     ┃ Status  ┃",
        "┡━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━┩",
    ]
    lines.extend(
        f"│ {name} │ {category} │ {source} │ {trust} │ {status} │"
        for name, category, source, trust, status in rows
    )
    lines.extend(
        [
            "└────────┴──────────┴────────┴───────────┴─────────┘",
            f"{hub} hub-installed, {builtin} builtin, {local} local — "
            f"{len(rows)} enabled, 0 disabled",
        ]
    )
    return "\n".join(lines) + "\n"


class _HTTPTransport:
    def __init__(self, responses: list[MarketplaceHTTPResponse]):
        self.responses = responses
        self.requests: list[dict] = []

    async def request(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class MarketplaceGatewayClientTests(unittest.TestCase):
    def test_redeem_verifies_exact_signed_manifest_and_fetches_only_fixed_release_path(
        self,
    ) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519

        from loopdy_plugin.link_client import LinkRuntimeConfig

        package = _skill_package()
        package_hash = hashlib.sha256(package).hexdigest()
        item_id = "itm_01K4CV8BDQ8JH3V4X2Y6Z7W9AA"
        artifact_path = f"/v1/marketplace/items/{item_id}/versions/1/artifact"
        listing = {
            "id": item_id,
            "kind": "skill",
            "name": "Focus Helper",
            "summary": "A bounded focus helper.",
            "description": "Synthetic marketplace test skill.",
            "author": "Fixture Author",
            "publisherId": "pub_01K4CV8BDQ8JH3V4X2Y6Z7W9AA",
            "license": "MIT",
            "version": 1,
            "sha256": package_hash,
            "byteCount": len(package),
            "minimumAppVersion": "1.0.0",
            "requiredCapabilities": [],
            "tags": ["focus"],
            "publishedAt": "2026-09-05T00:00:00.000Z",
            "preview": {},
        }
        manifest = json.dumps(
            {
                "schemaVersion": 1,
                "item": listing,
                "artifactPath": artifact_path,
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        signing_key = ed25519.Ed25519PrivateKey.generate()
        signature = signing_key.sign(manifest)
        envelope = json.dumps(
            {
                "keyId": "marketplace-test-v1",
                "manifestBase64": base64.b64encode(manifest).decode("ascii"),
                "signatureBase64": base64.b64encode(signature).decode("ascii"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        transport = _HTTPTransport(
            [
                MarketplaceHTTPResponse(200, {"content-type": "application/json"}, b"{}"),
                MarketplaceHTTPResponse(200, {"content-type": "application/json"}, envelope),
                MarketplaceHTTPResponse(200, {"content-type": "application/json"}, package),
            ]
        )
        config = LinkRuntimeConfig(
            base_url="https://link.example.test",
            device_id="host_device_fixture",
            authorization_epoch=2,
            signing_private_key=ec.generate_private_key(ec.SECP256R1()),
            account_key=b"a" * 32,
        )
        client = MarketplaceGatewayClient(
            config=config,
            trust_keys={"marketplace-test-v1": signing_key.public_key()},
            transport=transport,
        )
        payload = {
            "agentId": "research",
            "itemId": item_id,
            "version": 1,
            "sha256": package_hash,
            "approvalId": "apr_01K4CX5S4D8N2M6Q9V3Z7RTKPA",
            "requestId": "req_01K4CX5S4D8N2M6Q9V3Z7RTKPA",
        }

        actual = asyncio.run(client.redeem_and_fetch_skill(payload))

        self.assertEqual(actual, package)
        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                "https://link.example.test/v1/marketplace/install-approvals/"
                "apr_01K4CX5S4D8N2M6Q9V3Z7RTKPA/redeem",
                "https://link.example.test/v1/marketplace/items/"
                f"{item_id}/versions/1/manifest",
                "https://link.example.test" + artifact_path,
            ],
        )
        redeem = transport.requests[0]
        self.assertIn("x-loopdy-signature", redeem["headers"])
        self.assertEqual(
            json.loads(redeem["body"]),
            {key: payload[key] for key in ("agentId", "itemId", "version", "sha256", "requestId")},
        )


class MarketplaceSkillPackageTests(unittest.TestCase):
    def test_strict_package_rejects_traversal_collisions_archives_and_binary_files(
        self,
    ) -> None:
        base = json.loads(_skill_package())
        variants: list[tuple[str, bytes]] = []

        traversal = json.loads(_skill_package())
        traversal["content"]["files"] = {
            "references/../../outside.md": base64.b64encode(b"no").decode()
        }
        variants.append(("unsafe path", json.dumps(traversal).encode()))

        collision = json.loads(_skill_package())
        collision["content"]["files"] = {
            "references/Guide.md": base64.b64encode(b"one").decode(),
            "references/guide.md": base64.b64encode(b"two").decode(),
        }
        variants.append(("colliding", json.dumps(collision).encode()))

        archive = json.loads(_skill_package())
        archive["content"]["files"] = {
            "assets/payload.zip": base64.b64encode(b"PK fixture").decode()
        }
        variants.append(("Nested archives", json.dumps(archive).encode()))

        for path in ("assets/installer.dmg", "scripts/helper.exe", "assets/module.wasm"):
            forbidden = json.loads(_skill_package())
            forbidden["content"]["files"] = {
                path: base64.b64encode(b"not an executable").decode()
            }
            variants.append(("forbidden", json.dumps(forbidden).encode()))

        binary = json.loads(_skill_package())
        binary["content"]["files"] = {
            "assets/data.bin": base64.b64encode(b"\xff\x00").decode()
        }
        variants.append(("UTF-8", json.dumps(binary).encode()))

        duplicate = _skill_package().replace(
            b'{"schemaVersion":1,', b'{"schemaVersion":1,"schemaVersion":1,', 1
        )
        variants.append(("duplicate", duplicate))

        for expected, value in variants:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(MarketplaceInstallError, expected):
                    parse_skill_package(value)


class MarketplaceSkillInstallTests(unittest.TestCase):
    def test_capability_requires_the_supported_hub_contract(self) -> None:
        self.assertEqual(MARKETPLACE_SKILL_CAPABILITY, "marketplace-skills-hub-v1")

    def test_hub_cli_uses_argv_selected_profile_without_force_and_parses_presence(self) -> None:
        calls: list[tuple[list[str], dict]] = []
        responses = [
            SimpleNamespace(returncode=0, stdout="Profile: Research", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout=_skills_list_output(
                    [("focus-helper", "", "well-known", "community", "enabled")],
                    hub=1,
                ),
                stderr="",
            ),
            SimpleNamespace(returncode=0, stdout="Installed: focus-helper", stderr=""),
        ]

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return responses.pop(0)

        hub = HermesHubCLI(executable="/fixture/hermes", runner=runner)
        asyncio.run(hub.validate_profile("research"))
        self.assertTrue(asyncio.run(hub.contains("research", "focus-helper")))
        asyncio.run(
            hub.install(
                "research",
                "well-known:https://link.test/.well-known/skills/marketplace/"
                "itm_01K4CV8BDQ8JH3V4X2Y6Z7W9AA/versions/1/focus-helper",
                "focus-helper",
            )
        )

        self.assertEqual(calls[0][0], ["/fixture/hermes", "profile", "show", "research"])
        self.assertEqual(
            calls[1][0],
            [
                "/fixture/hermes", "-p", "research", "skills", "list",
                "--source", "hub",
            ],
        )
        self.assertEqual(calls[2][0][-2:], [
            "well-known:https://link.test/.well-known/skills/marketplace/"
            "itm_01K4CV8BDQ8JH3V4X2Y6Z7W9AA/versions/1/focus-helper",
            "--yes",
        ])
        self.assertNotIn("--force", calls[2][0])
        self.assertTrue(all(call[1]["capture_output"] for call in calls))

        non_name_cells = HermesHubCLI(
            executable="/fixture/hermes",
            runner=lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=_skills_list_output(
                    [("actual-skill", "community", "local", "hub", "enabled")],
                    local=1,
                ),
                stderr="",
            ),
        )
        for false_name in ("community", "local", "hub", "enabled", "hub-installed"):
            with self.subTest(false_name=false_name):
                self.assertFalse(
                    asyncio.run(non_name_cells.contains("research", false_name))
                )

        maximum_name = "a" + "x" * 63
        maximum = HermesHubCLI(
            executable="/fixture/hermes",
            runner=lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=_skills_list_output(
                    [(maximum_name, "", "well-known", "community", "enabled")],
                    hub=1,
                ),
                stderr="",
            ),
        )
        self.assertTrue(asyncio.run(maximum.contains("research", maximum_name)))

    def test_hub_cli_fails_closed_for_truncated_or_ambiguous_name_rows(self) -> None:
        maximum_name = "a" + "x" * 63
        outputs = (
            _skills_list_output(
                [(maximum_name[:-2] + "…", "", "well-known", "community", "enabled")],
                hub=1,
            ),
            _skills_list_output(
                [
                    (maximum_name, "", "well-known", "community", "enabled"),
                    (maximum_name, "", "well-known", "community", "enabled"),
                ],
                hub=2,
            ),
        )
        for output in outputs:
            with self.subTest(output=output):
                client = HermesHubCLI(
                    executable="/fixture/hermes",
                    runner=lambda *_args, **_kwargs: SimpleNamespace(
                        returncode=0, stdout=output, stderr=""
                    ),
                )
                with self.assertRaisesRegex(
                    MarketplaceInstallError, "status output is ambiguous"
                ):
                    asyncio.run(client.contains("research", maximum_name))

    def test_hub_cli_surfaces_scanner_and_collision_refusal(self) -> None:
        for output, expected in (
            ("BLOCKED: dangerous skill", "scanner blocked"),
            ("Warning: 'focus-helper' is already installed", "already exists"),
            ("Finding: Installed: focus-helper", "rejected"),
        ):
            with self.subTest(output=output):
                hub = HermesHubCLI(
                    executable="/fixture/hermes",
                    runner=lambda *_args, **_kwargs: SimpleNamespace(
                        returncode=0, stdout=output, stderr=""
                    ),
                )
                with self.assertRaisesRegex(MarketplaceInstallError, expected):
                    asyncio.run(hub.install("research", "well-known:https://example.test/x", "focus-helper"))

    def test_install_redeems_then_uses_selected_profile_hub_without_installed_digest(self) -> None:
        package = _skill_package()
        digest = hashlib.sha256(package).hexdigest()
        payload = {
            "agentId": "research_success",
            "itemId": "itm_01K4CV8BDQ8JH3V4X2Y6Z7W9AA",
            "version": 1,
            "sha256": digest,
            "approvalId": "apr_01K4CX5S4D8N2M6Q9V3Z7RTKPA",
            "requestId": "req_01K4CX5S4D8N2M6Q9V3Z7RTKPA",
        }

        with tempfile.TemporaryDirectory() as directory:
            store = BighelpStore(Path(directory) / "loopdy.sqlite3")
            client = _ReleaseClient(package)
            hub = _HubClient()
            installer = MarketplaceSkillInstaller(
                store=store,
                release_client=client,
                hub_client=hub,
                marketplace_base_url="https://link.example.test",
            )

            installed = asyncio.run(installer.install(payload))
            replayed = asyncio.run(installer.install(payload))
            status = asyncio.run(
                installer.status(
                    {"agentId": "research_success", "itemId": payload["itemId"]}
                )
            )

            self.assertTrue(installed["installed"])
            self.assertTrue(installed["changed"])
            self.assertEqual(installed["status"], "installed")
            self.assertEqual(installed["verificationMode"], "hermes_hub")
            self.assertEqual(installed["sha256"], digest)
            self.assertNotIn("modified", installed)
            self.assertNotIn("contentSha256", installed)
            self.assertNotIn("files", installed)
            self.assertEqual(
                status,
                {
                    "agentId": "research_success",
                    "itemId": payload["itemId"],
                    "version": 1,
                    "sha256": digest,
                    "skillName": "focus-helper",
                    "installed": True,
                    "status": "installed",
                    "verificationMode": "hermes_hub",
                },
            )
            self.assertFalse(replayed["changed"])
            self.assertEqual(client.requests, [payload])
            self.assertEqual(
                hub.calls[:4],
                [
                    ("profile", "research_success"),
                    ("contains", "research_success", "focus-helper", "all"),
                    (
                        "install",
                        "research_success",
                        "well-known:https://link.example.test/.well-known/skills/marketplace/"
                        "itm_01K4CV8BDQ8JH3V4X2Y6Z7W9AA/versions/1/focus-helper",
                        "focus-helper",
                    ),
                    ("contains", "research_success", "focus-helper", "hub"),
                ],
            )

    def test_status_without_receipt_omits_release_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = MarketplaceSkillInstaller(
                store=BighelpStore(Path(directory) / "loopdy.sqlite3"),
                release_client=_ReleaseClient(_skill_package()),
                hub_client=_HubClient(),
                marketplace_base_url="https://link.example.test",
            )

            status = asyncio.run(
                installer.status(
                    {
                        "agentId": "research_empty",
                        "itemId": "itm_01K4CV8BDQ8JH3V4X2Y6Z7W9AA",
                    }
                )
            )

            self.assertEqual(
                status,
                {
                    "agentId": "research_empty",
                    "itemId": "itm_01K4CV8BDQ8JH3V4X2Y6Z7W9AA",
                    "installed": False,
                    "status": "not_installed",
                    "verificationMode": "hermes_hub",
                },
            )

    def test_hub_scanner_or_collision_rejection_never_persists_a_receipt(self) -> None:
        package = _skill_package()
        digest = hashlib.sha256(package).hexdigest()
        payload = {
            "agentId": "research_failure",
            "itemId": "itm_01K4CV8BDQ8JH3V4X2Y6Z7W9AA",
            "version": 1,
            "sha256": digest,
            "approvalId": "apr_01K4CX5S4D8N2M6Q9V3Z7RTKPA",
            "requestId": "req_01K4CX5S4D8N2M6Q9V3Z7RTKPA",
        }

        for failure in (
            "Hermes Hub scanner blocked installation",
            "A skill with this name already exists; it was not replaced",
        ):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                store = BighelpStore(Path(directory) / "loopdy.sqlite3")
                installer = MarketplaceSkillInstaller(
                    store=store,
                    release_client=_ReleaseClient(package),
                    hub_client=_HubClient(install_error=failure),
                    marketplace_base_url="https://link.example.test",
                )
                with self.assertRaisesRegex(MarketplaceInstallError, failure):
                    asyncio.run(installer.install(payload))
                self.assertIsNone(
                    store.get_marketplace_skill_install(
                        profile="research_failure", item_id=payload["itemId"]
                    )
                )

    def test_marketplace_module_has_no_private_hermes_install_or_readback_calls(self) -> None:
        source = (PLUGIN_ROOT / "loopdy_plugin" / "marketplace.py").read_text(encoding="utf-8")
        for forbidden in (
            "_profile_scope",
            "_resolve_profile_dir",
            "_find_skill",
            "_preflight_scan",
            "skill_manage(",
            "skill_manager_tool",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
