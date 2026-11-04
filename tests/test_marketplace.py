"""The Plugins page's marketplace facts: the feed's stars, featured ranks and dates, read at most once in 6
hours and kept through a failed read, and Hermes' own refusal for each catalog entry it won't install here.
The refusals come from Hermes' real install check on real catalog entries.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

import telemetry_off  # noqa: F401
from loopdy_plugin import marketplace
import test_native_api as native_fixtures
from test_native_api import PREFIX

FEED = [
    {"name": "lantern", "stars": 4120, "featured": 2, "addedAt": "2026-09-20T15:06:49-07:00",
     "updatedAt": "2026-11-03T11:49:15-08:00", "tier": "official"},
    {"name": "quill", "stars": None, "addedAt": "2026-10-01T08:00:00Z"},
    {"name": "lantern", "stars": 1},
    {"name": "Bad Name", "stars": 7},
    {"name": "moss", "stars": True, "featured": 0, "addedAt": "yesterday", "updatedAt": "2026-10-02T08:00:00"},
    "not a row",
]


def hermes_catalog_available() -> bool:
    try:
        from hermes_cli.plugin_catalog import entry_from_mapping  # noqa: F401
        from hermes_cli.plugins_cmd_catalog import refuse_incompatible_catalog_entry  # noqa: F401
    except Exception:
        return False
    return True


class FeedTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.saved = Path(folder.name) / "plugin-data" / "loopdy" / "marketplace-plugins.json"
        self.stack = [patch.object(marketplace, "_saved_path", return_value=self.saved),
                      patch.object(marketplace, "_install_refusals", return_value={})]
        for item in self.stack:
            item.start()
            self.addCleanup(item.stop)
        marketplace.forget()
        self.addCleanup(marketplace.forget)
        self.clock = [1_800_000_000.0]
        clock = patch.object(marketplace.time, "time", side_effect=lambda: self.clock[0])
        clock.start()
        self.addCleanup(clock.stop)

    def test_feed_rows_keep_good_fields_and_leave_out_bad_ones(self):
        self.assertEqual(marketplace.facts_from_feed(FEED), {
            "lantern": {"stars": 4120, "featured": 2, "addedAt": "2026-09-20T22:06:49Z",
                        "updatedAt": "2026-11-03T19:49:15Z"},
            "quill": {"addedAt": "2026-10-01T08:00:00Z"},
            "moss": {},
        })
        with self.assertRaises(ValueError):
            marketplace.facts_from_feed({"entries": []})

    def test_feed_is_read_once_in_six_hours_and_kept_when_a_read_fails(self):
        with patch.object(marketplace, "_fetch", return_value=marketplace.facts_from_feed(FEED)) as fetch:
            first = marketplace.plugins()
            self.clock[0] += marketplace.FEED_TTL_SECONDS - 1
            self.assertEqual(marketplace.plugins(), first)
        fetch.assert_called_once()
        self.assertEqual(first["fetchedAt"], "2027-01-15T08:00:00Z")
        self.assertEqual([row["name"] for row in first["plugins"]], ["lantern", "moss", "quill"])
        self.assertEqual(first["plugins"][0], {"name": "lantern", "stars": 4120, "featured": 2,
                                               "addedAt": "2026-09-20T22:06:49Z",
                                               "updatedAt": "2026-11-03T19:49:15Z"})

        self.clock[0] += 2
        with patch.object(marketplace, "_fetch", side_effect=OSError("offline")) as fetch:
            self.assertEqual(marketplace.plugins(), first, "The last good copy stays, whatever its age")
            self.clock[0] += 10
            marketplace.plugins()
        fetch.assert_called_once()  # a failed read waits a minute before the next try

    def test_a_restarted_process_answers_from_the_saved_copy(self):
        with patch.object(marketplace, "_fetch", return_value=marketplace.facts_from_feed(FEED)):
            first = marketplace.plugins()
        self.assertTrue(self.saved.is_file())
        self.assertEqual(list(self.saved.parent.iterdir()), [self.saved], "No temporary file is left")
        marketplace.forget()
        self.clock[0] += 60
        with patch.object(marketplace, "_fetch", side_effect=AssertionError("read again")):
            self.assertEqual(marketplace.plugins(), first)

    def test_without_a_feed_or_a_saved_copy_the_answer_is_empty(self):
        with patch.object(marketplace, "_fetch", side_effect=ValueError("changed")):
            self.assertEqual(marketplace.plugins(), {"fetchedAt": None, "plugins": []})

    def test_redirects_stay_on_https(self):
        handler = marketplace._HTTPSRedirectsOnly()
        request = Request(marketplace.FEED_URL)
        self.assertIsNone(handler.redirect_request(request, None, 301, "Moved", {}, "http://example.invalid/plugins.json"))
        moved = handler.redirect_request(request, None, 301, "Moved", {}, "https://example.invalid/plugins.json")
        self.assertEqual(moved.full_url, "https://example.invalid/plugins.json")


@unittest.skipUnless(hermes_catalog_available(), "this Hermes has no catalog install check")
class RefusalTests(unittest.TestCase):
    def setUp(self):
        marketplace.forget()
        self.addCleanup(marketplace.forget)

    @staticmethod
    def entry(name, **fields):
        from hermes_cli.plugin_catalog import entry_from_mapping
        entry = entry_from_mapping({"name": name, "repo": f"https://github.com/example/{name}", "sha": "a" * 40,
                                    "description": "A made-up plugin.", "maintainer": "example", **fields}, name)
        assert entry is not None, fields
        return entry

    def test_entries_hermes_would_refuse_carry_its_own_words(self):
        entries = [self.entry("fits"), self.entry("too-new", requires_hermes=">=999.0"),
                   self.entry("elsewhere", platforms=["plan9"])]
        # A test checkout has no release stamp, and Hermes never refuses for an unknown version.
        with patch("hermes_cli.plugin_catalog.load_catalog_live", return_value=entries), \
                patch("hermes_cli.plugins_manifest.running_hermes_version", return_value="0.22.6"):
            refusals = marketplace._install_refusals()
        self.assertEqual(set(refusals), {"too-new", "elsewhere"})
        self.assertIn("too-new needs Hermes >=999.0; this is 0.22.6.", refusals["too-new"])
        self.assertIn("Update Hermes", refusals["too-new"])
        self.assertIn("plan9", refusals["elsewhere"])

    def test_a_catalog_hermes_cannot_read_refuses_nothing(self):
        with patch("hermes_cli.plugin_catalog.load_catalog_live", side_effect=OSError("unreadable")):
            self.assertEqual(marketplace._install_refusals(), {})


class MarketplaceRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def call(self, body):
        return self.fixture.client.post(PREFIX + "/marketplace/plugins", json=body, headers=self.fixture.headers())

    def test_route_answers_and_refuses_bad_requests(self):
        if not marketplace.available():
            self.skipTest("this Hermes has no plugin catalog")
        self.assertIn(marketplace.CAPABILITY, self.fixture.context().json()["features"])
        answer = {"fetchedAt": "2027-01-15T08:00:00Z",
                  "plugins": [{"name": "too-new", "installRefusal": "too-new needs Hermes >=999.0; this is 0.1.0."}]}
        with patch.object(marketplace, "plugins", return_value=answer):
            result = self.call({})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), answer)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(self.call({"names": ["too-new"]}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
