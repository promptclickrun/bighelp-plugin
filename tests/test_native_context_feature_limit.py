"""Apps released before the full feature list refuse a context that lists more than 64 features.

The plugin keeps them working: a context request without `X-Bighelp-Features: all` gets at most 64,
and the features added after the list reached 64 (`LATE_FEATURES`) are the ones left out, because
those apps don't know them. Newer apps ask for the full list.
"""

from __future__ import annotations

import unittest

import telemetry_off  # noqa: F401
from loopdy_plugin import native_context
from loopdy_plugin.native_context import LATE_FEATURES, LEGACY_FEATURE_LIMIT, NativeContext


def _context(features: tuple[str, ...]) -> NativeContext:
    return NativeContext(provider=None, user_id=None, display_name=None, serving_profile_id="default",
                         features=features, runtime_id="runtime-fixture")


class FeatureLimitTests(unittest.TestCase):
    def test_every_feature_older_apps_know_fits_in_their_list(self) -> None:
        """Each feature is either listed or skipped with a reason, so the two together are every feature the
        plugin can list. A new feature past the first 64 must be in LATE_FEATURES, or an older app would lose
        one it knows when the list is cut."""
        skipped: list[tuple[str, str]] = []
        _profile, advertised = native_context._native_features(skipped=skipped)
        every = list(dict.fromkeys([*advertised, *(name for name, _reason in skipped)]))
        self.assertTrue(set(LATE_FEATURES) <= set(every), "LATE_FEATURES names only features the plugin lists")
        known = [name for name in every if name not in LATE_FEATURES]
        self.assertLessEqual(
            len(known), LEGACY_FEATURE_LIMIT,
            "More than 64 features outside LATE_FEATURES: add the newest ones to LATE_FEATURES "
            "in native_context.py.")
        self.assertEqual(_context(tuple(every)).payload(full=False)["features"], known)

    def test_older_apps_get_at_most_64_and_never_a_late_feature(self) -> None:
        late = LATE_FEATURES[0]
        known = tuple(f"native-fixture-{index}-v1" for index in range(LEGACY_FEATURE_LIMIT))
        context = _context(known[:10] + (late,) + known[10:])
        listed = context.payload(full=False)["features"]
        self.assertEqual(len(listed), LEGACY_FEATURE_LIMIT)
        self.assertNotIn(late, listed)
        self.assertEqual(listed, list(known), "Everything those apps know, in order")

    def test_newer_apps_get_every_feature_with_late_ones_last(self) -> None:
        late = LATE_FEATURES[0]
        known = tuple(f"native-fixture-{index}-v1" for index in range(LEGACY_FEATURE_LIMIT))
        context = _context((late,) + known)
        listed = context.payload()["features"]
        self.assertEqual(len(listed), LEGACY_FEATURE_LIMIT + 1)
        self.assertEqual(listed[-1], late)

    def test_a_short_list_is_never_cut(self) -> None:
        late = LATE_FEATURES[0]
        context = _context(("native-context-v1", late))
        self.assertEqual(context.payload(full=False)["features"], ["native-context-v1", late])

    def test_the_etag_is_the_full_contexts_whatever_list_was_sent(self) -> None:
        context = _context(tuple(f"native-fixture-{index}-v1" for index in range(70)))
        self.assertEqual(context.etag, _context(context.features).etag)
        self.assertNotEqual(context.payload(full=False), context.payload())

    def test_the_app_asks_for_every_feature_by_header(self) -> None:
        self.assertTrue(native_context.wants_every_feature({"x-bighelp-features": "all"}))
        self.assertTrue(native_context.wants_every_feature({"x-bighelp-features": "ALL"}))
        self.assertFalse(native_context.wants_every_feature({}))
        self.assertFalse(native_context.wants_every_feature({"x-bighelp-features": "some"}))


if __name__ == "__main__":
    unittest.main()
