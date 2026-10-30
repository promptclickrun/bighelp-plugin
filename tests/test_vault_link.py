"""Vault link: a saved card or address copied to more origins, on the host, with Hermes' own vault.

Runs on a scratch Hermes home with the real `VaultStore` (a fake would not prove the key and file of the
right agent are the ones written). Every refusal must leave the vault exactly as it was.
"""
from __future__ import annotations
import telemetry_off  # noqa: F401  (no test run posts host telemetry)

import dataclasses
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

from loopdy_plugin import native_api, vault_link
import test_native_api as native_fixtures
from test_native_api import PREFIX

CARD = {"card_number": "4242424242424242", "cardholder_name": "Pat Example", "exp_month": "04",
        "exp_year": "2031", "cvc": "737", "billing_postal_code": "00000"}
HOME = {"address_line1": "1 Example Way", "city": "Sampletown", "postal_code": "00000", "country": "Exampleland"}
LOGIN = {"identifier_type": "email", "identifier": "pat@example.invalid", "password": "correct-horse-battery"}
SHOP = "https://shop.example.org"
OTHER_SHOP = "https://store.example.net"


@unittest.skipUnless(vault_link.available(), "this Hermes has no vault")
class VaultLinkTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home
        self.research = self.home / "profiles/research"

    def vault(self, profile=None):
        """A store on the profile's own home, made the way Hermes makes it: from its home folder."""
        from agent.vault_store import VaultStore
        return VaultStore((self.research if profile else self.home) / "vault")

    def save(self, kind, label, secret, origin=None, profile=None):
        return self.vault(profile).add_item(kind, label, dict(secret), origin).id

    def call(self, body, headers=None):
        return self.fixture.client.post(PREFIX + "/vault/link", json=body,
                                        headers=self.fixture.headers() if headers is None else headers)

    def snapshot(self, profile=None):
        store = self.vault(profile)
        return sorted((meta.id, meta.kind, meta.label, meta.origin, tuple(sorted(store.resolve_secret(meta.id).items())))
                      for meta in store.list_items())

    def test_a_card_is_copied_to_two_new_origins_with_its_secret(self):
        card = self.save("payment", "Everyday card", CARD, SHOP)
        result = self.call({"item_ids": [card], "origins": [OTHER_SHOP + ":443/checkout", "http://Pay.Example.com:8080"]})
        self.assertEqual(result.status_code, 200, result.text)
        copies = result.json()["copies"]
        self.assertEqual([(c["item_id"], c["origin"], c["status"]) for c in copies],
                         [(card, OTHER_SHOP, "created"), (card, "http://pay.example.com:8080", "created")])
        self.assertEqual(result.headers["cache-control"], "no-store")
        store = self.vault()
        for copy in copies:
            self.assertTrue(copy["id"].startswith("vault_") and copy["id"] != card)
            meta = store.get_meta(copy["id"])
            self.assertEqual((meta.kind, meta.label, meta.origin), ("payment", "Everyday card", copy["origin"]))
            self.assertEqual(store.resolve_secret(copy["id"]), CARD)
        self.assertEqual(len(store.list_items()), 3)
        self.assertNotIn(CARD["card_number"], result.text)
        self.assertNotIn(CARD["cvc"], result.text)

    def test_a_retry_reports_the_copies_that_exist_and_makes_no_duplicates(self):
        card = self.save("payment", "Everyday card", CARD, SHOP)
        home = self.save("address", "Home", HOME)  # an item with no origin yet
        body = {"item_ids": [card, home], "origins": [SHOP, OTHER_SHOP]}
        first = self.call(body).json()["copies"]
        self.assertEqual([(c["item_id"], c["origin"], c["status"]) for c in first],
                         [(card, SHOP, "existing"), (card, OTHER_SHOP, "created"),
                          (home, SHOP, "created"), (home, OTHER_SHOP, "created")])
        self.assertEqual(first[0]["id"], card, "the source's own origin is the item itself")
        after_first = self.snapshot()
        second = self.call(body).json()["copies"]
        self.assertEqual([c["status"] for c in second], ["existing"] * 4)
        self.assertEqual([c["id"] for c in second], [c["id"] for c in first])
        self.assertEqual(self.snapshot(), after_first)

    def test_a_same_label_item_with_another_secret_is_not_taken_for_the_copy(self):
        card = self.save("payment", "Everyday card", CARD, SHOP)
        self.save("payment", "Everyday card", {**CARD, "cvc": "111"}, OTHER_SHOP)
        copies = self.call({"item_ids": [card], "origins": [OTHER_SHOP]}).json()["copies"]
        self.assertEqual(copies[0]["status"], "created")
        self.assertEqual(self.vault().resolve_secret(copies[0]["id"]), CARD)

    def test_two_identical_sources_make_one_copy(self):
        one = self.save("payment", "Card", CARD)
        two = self.save("payment", "Card", CARD)
        copies = self.call({"item_ids": [one, two], "origins": [SHOP]}).json()["copies"]
        self.assertEqual([c["status"] for c in copies], ["created", "existing"])
        self.assertEqual(copies[0]["id"], copies[1]["id"])

    def test_refusals_change_nothing(self):
        card = self.save("payment", "Card", CARD, SHOP)
        login = self.save("login", "Shop login", LOGIN, SHOP)
        before = self.snapshot()
        cases = (
            ("login", {"item_ids": [card, login], "origins": [OTHER_SHOP]}, 400, "not_copyable"),
            ("not in this vault", {"item_ids": [card, "op:vault-item-1"], "origins": [OTHER_SHOP]}, 400, "not_copyable"),
            ("unknown id after a good one", {"item_ids": [card, "vault_0123456789ab"], "origins": [OTHER_SHOP]}, 404, "item_not_found"),
            ("unknown profile", {"profile": "ghost", "item_ids": [card], "origins": [OTHER_SHOP]}, 404, "profile_not_found"),
        )
        for label, body, status, code in cases:
            with self.subTest(label):
                result = self.call(body)
                self.assertEqual((result.status_code, result.json()["error"]["code"]), (status, code), result.text)
                self.assertEqual(self.snapshot(), before)
                self.assertNotIn(CARD["card_number"], result.text)
                self.assertNotIn(LOGIN["password"], result.text)

    def test_bad_requests_are_400_and_change_nothing(self):
        card = self.save("payment", "Card", CARD)
        before = self.snapshot()
        good = {"item_ids": [card], "origins": [SHOP]}
        for label, body in (
            ("unknown field", {**good, "extra": 1}),
            ("no items", {**good, "item_ids": []}),
            ("no origins", {**good, "origins": []}),
            ("eleven items", {**good, "item_ids": [f"vault_{n:012x}" for n in range(11)]}),
            ("eleven origins", {**good, "origins": [f"https://s{n}.example.org" for n in range(11)]}),
            ("same item twice", {**good, "item_ids": [card, card]}),
            ("same origin twice", {**good, "origins": [SHOP, SHOP + ":443/path"]}),
            ("no scheme", {**good, "origins": ["shop.example.org"]}),
            ("not http", {**good, "origins": ["ftp://shop.example.org"]}),
            ("origin not a string", {**good, "origins": [7]}),
            ("bad profile", {**good, "profile": "../x"}),
            ("profile ID that is a number", {**good, "profile": 7}),
        ):
            with self.subTest(label):
                result = self.call(body)
                self.assertEqual((result.status_code, result.json()["error"]["code"]), (400, "invalid_request"), result.text)
        self.assertEqual(self.snapshot(), before)
        with self.subTest("missing context"):
            self.assertEqual(self.call(good, {"Authorization": self.fixture.headers()["Authorization"]}).status_code, 428)

    def test_the_agents_own_vault_is_the_one_written(self):
        mine = self.save("payment", "Card", CARD, SHOP)
        theirs = self.save("payment", "Card", {**CARD, "cvc": "999"}, SHOP, profile="research")
        default_before = self.snapshot()
        result = self.call({"profile": "research", "item_ids": [theirs], "origins": [OTHER_SHOP]})
        self.assertEqual(result.status_code, 200, result.text)
        made = result.json()["copies"][0]["id"]
        self.assertEqual(self.vault("research").resolve_secret(made)["cvc"], "999")
        self.assertIsNone(self.vault().get_meta(made))
        self.assertEqual(self.snapshot(), default_before, "the serving agent's vault was not touched")
        # An item of another agent's vault is not found in this one.
        result = self.call({"item_ids": [theirs], "origins": [OTHER_SHOP]})
        self.assertEqual((result.status_code, result.json()["error"]["code"]), (404, "item_not_found"))
        # No profile and the serving agent's own ID both mean the serving agent.
        for body in ({"item_ids": [mine], "origins": [OTHER_SHOP]}, {"profile": "default", "item_ids": [mine], "origins": [OTHER_SHOP]}):
            with self.subTest(body.get("profile")):
                result = self.call(body)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertIn(result.json()["copies"][0]["status"], ("created", "existing"))
        self.assertEqual(len(self.vault().list_items()), 2)
        self.assertEqual(len(self.vault("research").list_items()), 2)

    def test_a_serving_agent_that_is_a_named_profile_is_the_default_when_profile_is_left_out(self):
        mine = self.save("payment", "Card", CARD, SHOP, profile="research")
        default_card = self.save("payment", "Card", CARD, SHOP)
        default_before = self.snapshot()
        with patch("hermes_constants.profile_name_for_home", return_value="research"):
            self.assertEqual(self.fixture.context().json()["servingProfileId"], "research")
            result = self.call({"item_ids": [mine], "origins": [OTHER_SHOP]})
            self.assertEqual(result.status_code, 200, result.text)
            # The default agent's item is not in the serving agent's vault.
            missing = self.call({"item_ids": [default_card], "origins": [OTHER_SHOP]})
        self.assertEqual(missing.json()["error"]["code"], "item_not_found")
        made = result.json()["copies"][0]["id"]
        self.assertEqual(self.vault("research").get_meta(made).origin, OTHER_SHOP)
        self.assertEqual(self.snapshot(), default_before, "the default agent's vault was not touched")

    def test_two_overlapping_requests_make_one_copy(self):
        """The app retries at once when an answer is lost, so a retry can overlap the first request."""
        from agent.vault_store import VaultStore
        card = self.save("payment", "Card", CARD)
        real = VaultStore.add_item
        first_write = threading.Event()

        def slow(store, *args, **kwargs):
            first_write.set()
            time.sleep(0.2)  # long enough for the second request to take its snapshot, were there no lock
            return real(store, *args, **kwargs)

        results, errors = [], []

        def run(wait):
            try:
                if wait:
                    first_write.wait(5)
                results.append(vault_link.link("default", [card], [SHOP]))
            except BaseException as error:  # a failing thread must fail the test, not vanish
                errors.append(error)

        with patch.object(VaultStore, "add_item", slow):
            threads = [threading.Thread(target=run, args=(False,)), threading.Thread(target=run, args=(True,))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(errors, [])
        copies = [value["copies"][0] for value in results]
        self.assertEqual(sorted(copy["status"] for copy in copies), ["created", "existing"])
        self.assertEqual(copies[0]["id"], copies[1]["id"])
        self.assertEqual(len(self.vault().list_items()), 2)

    def test_a_write_that_fails_midway_reports_the_copies_made_and_a_retry_finishes(self):
        card = self.save("payment", "Card", CARD)
        origins = [SHOP, OTHER_SHOP, "https://third.example.org"]
        real, calls = self.vault().add_item.__func__, []

        def flaky(store, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk full: " + CARD["card_number"])
            return real(store, *args, **kwargs)

        from agent.vault_store import VaultStore
        with patch.object(VaultStore, "add_item", flaky):
            result = self.call({"item_ids": [card], "origins": origins})
        error = result.json()["error"]
        self.assertEqual((result.status_code, error["code"]), (500, "vault_write_failed"))
        self.assertNotIn(CARD["card_number"], result.text)
        done = error["details"]["copies"]
        self.assertEqual([(c["origin"], c["status"]) for c in done], [(SHOP, "created")])
        retry = self.call({"item_ids": [card], "origins": origins}).json()["copies"]
        self.assertEqual([c["status"] for c in retry], ["existing", "created", "created"])
        self.assertEqual(retry[0]["id"], done[0]["id"])
        self.assertEqual(len(self.vault().list_items()), 4)

    def test_a_vault_that_cannot_be_opened_is_unavailable(self):
        card = self.save("payment", "Card", CARD)
        (self.home / "vault" / "vault.key").write_bytes(b"not-a-key")
        result = self.call({"item_ids": [card], "origins": [SHOP]})
        self.assertEqual((result.status_code, result.json()["error"]["code"]), (503, "vault_unavailable"), result.text)
        self.assertTrue(result.json()["error"]["retryable"])

    def test_a_context_change_after_the_write_says_the_copies_and_a_retry_is_safe(self):
        card = self.save("payment", "Card", CARD)
        headers = self.fixture.headers()
        real, calls = native_api.native_context, []

        def changed(request):
            calls.append(1)
            value = real(request)
            return value if len(calls) == 1 else dataclasses.replace(value, user_id="someone-else")

        with patch.object(native_api, "native_context", changed):
            result = self.call({"item_ids": [card], "origins": [SHOP]}, headers)
        error = result.json()["error"]
        self.assertEqual((result.status_code, error["code"]), (412, "context_changed"))
        self.assertEqual(error["details"]["copies"][0]["status"], "created")
        retry = self.call({"item_ids": [card], "origins": [SHOP]}).json()["copies"]
        self.assertEqual(retry[0]["status"], "existing")


class VaultLinkFeatureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_the_feature_is_listed_only_when_the_hermes_has_the_vault(self):
        for available in (True, False):
            with self.subTest(available=available), patch.object(vault_link, "available", return_value=available):
                features = self.fixture.context().json()["features"]
                self.assertEqual(vault_link.CAPABILITY in features, available)
        with patch.object(vault_link, "available", return_value=False):
            result = self.fixture.client.post(PREFIX + "/vault/link", headers=self.fixture.headers(),
                                              json={"item_ids": ["vault_0123456789ab"], "origins": [SHOP]})
        self.assertEqual((result.status_code, result.json()["error"]["code"]), (503, "vault_unavailable"))

    def test_a_hermes_without_the_vault_module_does_not_list_it(self):
        with patch.dict(sys.modules, {"agent.vault_store": None}):
            self.assertFalse(vault_link.available())
            self.assertNotIn(vault_link.CAPABILITY, self.fixture.context().json()["features"])


if __name__ == "__main__":
    unittest.main()
