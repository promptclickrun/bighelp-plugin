from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from loopdy_plugin.loopdy_cards import (
    BighelpCardError,
    render_card,
    validate_card_input,
    validate_card_result,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PLUGIN_ROOT / "fixtures" / "loopdy_card_v1"
SCHEMA_PATH = PLUGIN_ROOT / "spec" / "loopdy-card-v1.schema.json"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
SCENES = (
    "none",
    "clear",
    "partly_cloudy",
    "overcast",
    "rain",
    "thunderstorm",
    "snow",
    "fog",
    "wind",
)


def fixture(name: str = "static-metrics.json") -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def with_background(background: object, element: str = "card") -> dict[str, object]:
    card = fixture()
    card["elements"][element]["background"] = background
    return card


class CardBackgroundValidationTests(unittest.TestCase):
    def validate(self, value: object) -> dict[str, object]:
        return validate_card_input(value, now=NOW)

    def assert_code(self, code: str, value: object) -> None:
        with self.assertRaises(BighelpCardError) as caught:
            self.validate(value)
        self.assertEqual(code, caught.exception.code)

    def test_every_scene_intensity_and_time_of_day_is_accepted_unchanged(self) -> None:
        for scene in SCENES:
            for intensity in ("light", "moderate", "heavy"):
                for time_of_day in ("day", "dusk", "night"):
                    background = {"scene": scene, "intensity": intensity, "time_of_day": time_of_day}
                    with self.subTest(background=background):
                        card = with_background(background)
                        self.assertEqual(card, self.validate(copy.deepcopy(card)))

    def test_scene_alone_is_enough_and_defaults_are_left_to_the_app(self) -> None:
        card = with_background({"scene": "rain"})
        validated = self.validate(card)
        # The canonical document keeps what the agent wrote; the app applies
        # moderate and day, so the hash doesn't change when defaults do.
        self.assertEqual({"scene": "rain"}, validated["elements"]["card"]["background"])

    def test_cards_without_a_background_keep_their_exact_hash(self) -> None:
        rendered = render_card(fixture(), now=NOW)
        self.assertEqual(
            "bf3cc2d664a5e7d1067c5e50173d5e643642e41e8a0f6f9c2703e962ad43563a",
            rendered["content_hash"],
        )

    def test_a_rendered_background_survives_result_validation(self) -> None:
        rendered = render_card(with_background({"scene": "snow", "time_of_day": "night"}), now=NOW)
        self.assertEqual(rendered, validate_card_result(rendered, now=NOW))

    def test_unknown_values_warn_once_per_render_and_are_kept(self) -> None:
        card = with_background({"scene": "hail", "intensity": "extreme", "time_of_day": "dawn"})
        with self.assertLogs("loopdy_plugin.loopdy_cards", level="WARNING") as logs:
            rendered = render_card(card, now=NOW)
            validate_card_result(rendered, now=NOW)
        self.assertEqual(
            {"scene": "hail", "intensity": "extreme", "time_of_day": "dawn"},
            rendered["elements"]["card"]["background"],
        )
        self.assertEqual(3, len(logs.records), logs.output)
        output = "\n".join(logs.output)
        for field in ("scene", "intensity", "time_of_day"):
            self.assertIn(f"field={field}", output)
        # Log the field and a fixed code only, never the agent's text.
        self.assertNotIn("hail", output)
        self.assertNotIn("extreme", output)

    def test_known_values_log_nothing(self) -> None:
        with self.assertNoLogs("loopdy_plugin.loopdy_cards", level="WARNING"):
            render_card(with_background({"scene": "fog", "intensity": "light"}), now=NOW)

    def test_background_on_any_other_element_is_rejected(self) -> None:
        self.assert_code("invalid_background", with_background({"scene": "rain"}, element="metrics"))
        self.assert_code("invalid_background", with_background({"scene": "rain"}, element="passed"))

    def test_background_inside_props_is_rejected_with_a_pointer_to_the_right_place(self) -> None:
        card = fixture()
        card["elements"]["card"]["props"]["background"] = {"scene": "rain"}
        with self.assertRaises(BighelpCardError) as caught:
            self.validate(card)
        self.assertEqual("invalid_background", caught.exception.code)
        self.assertIn("beside props", str(caught.exception))

    def test_extra_keys_inside_background_are_rejected(self) -> None:
        self.assert_code("invalid_background", with_background({"scene": "rain", "color": "#000000"}))
        self.assert_code("invalid_background", with_background({"scene": "rain", "animated": False}))

    def test_malformed_backgrounds_are_rejected(self) -> None:
        cases = {
            "not an object": "rain",
            "list": ["rain"],
            "missing scene": {"intensity": "heavy"},
            "number scene": {"scene": 3},
            "null scene": {"scene": None},
            "empty scene": {"scene": ""},
            "upper case": {"scene": "Rain"},
            "spaces": {"scene": "light rain"},
            "too long": {"scene": "a" * 33},
            "boolean intensity": {"scene": "rain", "intensity": True},
            "object time": {"scene": "rain", "time_of_day": {"value": "day"}},
        }
        for label, background in cases.items():
            with self.subTest(case=label):
                self.assert_code("invalid_background", with_background(background))


class CardBackgroundSchemaTests(unittest.TestCase):
    def validator(self) -> Draft202012Validator:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)

    def test_the_portable_schema_matches_the_validator(self) -> None:
        validator = self.validator()
        accepted = [
            with_background({"scene": "rain"}),
            with_background({"scene": "clear", "intensity": "heavy", "time_of_day": "night"}),
            with_background({"scene": "hail"}),
            fixture("static-weather-background.json"),
        ]
        for card in accepted:
            with self.subTest(card=card["elements"]["card"].get("background")):
                self.assertEqual([], [error.message for error in validator.iter_errors(card)])
        rejected = [
            with_background({"scene": "rain"}, element="metrics"),
            with_background({"scene": "rain", "color": "#000000"}),
            with_background({"intensity": "heavy"}),
            with_background({"scene": 3}),
        ]
        for card in rejected:
            with self.subTest(card=card):
                self.assertTrue(list(validator.iter_errors(card)))

    def test_the_weather_fixture_is_a_valid_card(self) -> None:
        card = fixture("static-weather-background.json")
        self.assertEqual(card, validate_card_input(card, now=NOW))
        self.assertEqual("rain", card["elements"]["card"]["background"]["scene"])


if __name__ == "__main__":
    unittest.main()
