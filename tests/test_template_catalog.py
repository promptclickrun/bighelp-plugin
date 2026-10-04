from __future__ import annotations

import copy
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from loopdy_plugin import template_catalog
from loopdy_plugin.template_catalog import (
    CATALOG_URL,
    CatalogClient,
    CatalogUnavailable,
    FetchError,
    Response,
    fill,
    form_fields,
    parse_catalog,
)

import template_catalog_fixtures as fixtures


class _Transport:
    """Answers like catalog.bighelp.app: an ETag on 200, 304 when If-None-Match matches."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, headers, *, timeout, max_bytes):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout, "max_bytes": max_bytes})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _ok(document=None, etag='"rev-1"', url=CATALOG_URL, headers=None):
    return Response(status=200, headers={"etag": etag, **(headers or {})},
                    body=fixtures.body(document), url=url)


def _not_modified():
    return Response(status=304, headers={}, body=b"", url=CATALOG_URL)


class CatalogClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "template-catalog"
        self.now = [1_000_000.0]

    def client(self, transport):
        return CatalogClient(directory=self.directory, fetch=transport, clock=lambda: self.now[0])

    def test_first_read_fetches_exactly_the_catalog_and_keeps_a_copy(self):
        transport = _Transport(_ok())
        catalog = self.client(transport).catalog()

        self.assertEqual([a["id"] for a in catalog.agents],
                         ["release-coordinator", "garden-helper", "anchor"])
        self.assertEqual(transport.calls[0]["url"], "https://catalog.bighelp.app/v1/catalog.json")
        self.assertNotIn("If-None-Match", transport.calls[0]["headers"])
        self.assertEqual(transport.calls[0]["max_bytes"], 1_048_576)
        self.assertGreater(transport.calls[0]["timeout"], 0)
        saved = json.loads((self.directory / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["etag"], '"rev-1"')

    def test_refreshes_at_most_every_six_hours_with_the_etag(self):
        transport = _Transport(_ok(), _not_modified(), _ok(fixtures.catalog(revision="rev-2"), etag='"rev-2"'))
        client = self.client(transport)
        client.catalog()
        self.now[0] += 6 * 3600 - 1
        client.catalog()
        self.assertEqual(len(transport.calls), 1)

        self.now[0] += 1
        self.assertEqual(client.catalog().revision, "rev-1")
        self.assertEqual(transport.calls[1]["headers"]["If-None-Match"], '"rev-1"')
        # A 304 counts as a check, so the next one waits another six hours.
        self.now[0] += 3600
        client.catalog()
        self.assertEqual(len(transport.calls), 2)

        self.now[0] += 6 * 3600
        self.assertEqual(client.catalog().revision, "rev-2")
        self.assertEqual(transport.calls[2]["headers"]["If-None-Match"], '"rev-1"')

    def test_copy_survives_a_new_client_without_a_fetch(self):
        self.client(_Transport(_ok())).catalog()
        transport = _Transport()
        self.assertEqual(self.client(transport).catalog().revision, "rev-1")
        self.assertEqual(transport.calls, [])

    def test_failed_fetch_keeps_the_last_good_copy_and_backs_off(self):
        transport = _Transport(_ok(), FetchError("timeout"),
                               Response(status=500, headers={}, body=b"oops", url=CATALOG_URL),
                               _ok(fixtures.catalog(revision="rev-2")))
        client = self.client(transport)
        client.catalog()
        self.now[0] += 6 * 3600
        self.assertEqual(client.catalog().revision, "rev-1")
        # A failure isn't retried on every tool call.
        client.catalog()
        self.assertEqual(len(transport.calls), 2)
        self.now[0] += template_catalog.RETRY_SECONDS
        self.assertEqual(client.catalog().revision, "rev-1")
        self.now[0] += template_catalog.RETRY_SECONDS
        self.assertEqual(client.catalog().revision, "rev-2")

    def test_no_copy_and_no_answer_is_unavailable(self):
        client = self.client(_Transport(FetchError("offline")))
        with self.assertRaises(CatalogUnavailable):
            client.catalog()

    def test_answer_from_another_host_is_refused(self):
        for url in ("https://catalog.bighelp.app.evil.example/v1/catalog.json",
                    "http://catalog.bighelp.app/v1/catalog.json",
                    "https://evil.example/v1/catalog.json"):
            with self.subTest(url=url):
                self.directory = Path(tempfile.mkdtemp(dir=self.directory.parent))
                transport = _Transport(_ok(url=url))
                with self.assertRaises(CatalogUnavailable):
                    self.client(transport).catalog()
                self.assertFalse((self.directory / "catalog.json").exists())

    def test_redirects_only_stay_on_the_catalog_host(self):
        self.assertTrue(template_catalog.allowed_url("https://catalog.bighelp.app/v1/catalog.json"))
        for url in ("https://evil.example/v1/catalog.json", "http://catalog.bighelp.app/v1/catalog.json",
                    "https://catalog.bighelp.app:8443/v1/catalog.json",
                    "https://someone@catalog.bighelp.app/v1/catalog.json",
                    "https://catalog.bighelp.app.evil.example/v1/catalog.json", "file:///etc/hosts"):
            with self.subTest(url=url):
                self.assertFalse(template_catalog.allowed_url(url))
        handler = template_catalog._CatalogRedirects()
        request = urllib.request.Request(CATALOG_URL)
        with self.assertRaises(FetchError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/v1/catalog.json")
        followed = handler.redirect_request(request, None, 302, "Found", {},
                                            "https://catalog.bighelp.app/v1/catalog.json?moved=1")
        self.assertEqual(followed.full_url, "https://catalog.bighelp.app/v1/catalog.json?moved=1")

    def test_real_transport_refuses_other_hosts_before_connecting(self):
        with self.assertRaises(FetchError):
            template_catalog.https_fetch("http://catalog.bighelp.app/v1/catalog.json", {},
                                         timeout=1, max_bytes=10)
        with self.assertRaises(FetchError):
            template_catalog.https_fetch("https://evil.example/v1/catalog.json", {}, timeout=1, max_bytes=10)

    def test_oversize_answer_is_refused_and_the_copy_kept(self):
        too_big = b"{" + b" " * template_catalog.MAX_CATALOG_BYTES + b"}"
        transport = _Transport(_ok(), Response(status=200, headers={"etag": '"big"'}, body=too_big, url=CATALOG_URL),
                               _ok(headers={"content-length": str(template_catalog.MAX_CATALOG_BYTES + 1)}))
        client = self.client(transport)
        client.catalog()
        self.now[0] += 6 * 3600
        self.assertEqual(client.catalog().revision, "rev-1")
        self.now[0] += template_catalog.RETRY_SECONDS
        self.assertEqual(client.catalog().revision, "rev-1")
        saved = json.loads((self.directory / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["etag"], '"rev-1"')

    def test_answers_that_are_not_a_catalog_keep_the_copy(self):
        transport = _Transport(_ok(), Response(status=200, headers={}, body=b"<html>", url=CATALOG_URL))
        client = self.client(transport)
        client.catalog()
        self.now[0] += 6 * 3600
        self.assertEqual(client.catalog().revision, "rev-1")

    def test_damaged_copy_is_fetched_again(self):
        self.directory.mkdir(parents=True)
        (self.directory / "catalog.json").write_text("{not json", encoding="utf-8")
        transport = _Transport(_ok())
        self.assertEqual(self.client(transport).catalog().revision, "rev-1")
        self.assertEqual(len(transport.calls), 1)


class ParsingTests(unittest.TestCase):
    def test_unknown_keys_and_broken_entries_are_skipped(self):
        broken = fixtures.catalog(future={"x": 1})
        broken["agents"] += [
            {"id": "no-text", "name": "Nothing"},
            "not an entry",
            {"id": "release-coordinator", "name": "Duplicate", "instructions": "Again"},
            {"id": "Bad Id!", "name": "Bad", "instructions": "Hi {{agent_name}}"},
            {"id": "huge", "name": "Huge", "instructions": "x" * 20_001},
        ]
        broken["agents"][1]["newField"] = {"nested": True}
        catalog = parse_catalog(broken)
        self.assertEqual([a["id"] for a in catalog.agents], ["release-coordinator", "garden-helper", "anchor"])
        self.assertEqual(len(catalog.blueprints), 2)
        self.assertNotIn("newField", catalog.agents[1])

    def test_missing_lists_are_empty(self):
        catalog = parse_catalog({"schemaVersion": 1, "agents": "nope"})
        self.assertEqual((catalog.agents, catalog.blueprints), ([], []))
        with self.assertRaises(ValueError):
            parse_catalog(["not", "an", "object"])

    def test_community_text_is_plain_text(self):
        document = fixtures.catalog()
        document["agents"][1]["vibe"] = "Kind\x00\x1b[31m\u202e"
        catalog = parse_catalog(document)
        self.assertEqual(catalog.agents[1]["vibe"], "Kind[31m")

    def test_variables_follow_the_contract_leniently(self):
        template = copy.deepcopy(fixtures.COORDINATOR)
        template["variables"] = [
            {"key": "agent_role", "label": "Role", "type": "text", "maxLength": 5000},
            {"key": "Bad Key", "label": "Bad", "type": "text"},
            {"key": "agent_name", "label": "Reserved", "type": "text"},
            {"key": "agent_role", "label": "Duplicate", "type": "text"},
            {"key": "operating_context", "label": "Where", "type": "long_text", "maxLength": 9000,
             "required": False, "whenEmpty": "Anywhere."},
            {"key": "tone", "label": "Tone", "type": "choice", "options": ["Only one"], "default": "Only one"},
            {"key": "mood", "label": "Mood", "type": "choice", "options": ["Up", "Down"], "default": "Sideways"},
            {"key": "odd", "label": "Odd", "type": "colour", "required": True, "whenEmpty": "ignored"},
        ] + [{"key": f"extra_{n}", "label": f"Extra {n}", "type": "text"} for n in range(12)]
        variables = parse_catalog(fixtures.catalog(agents=[template])).agents[0]["variables"]

        self.assertEqual(len(variables), 12)
        by_key = {v["key"]: v for v in variables}
        self.assertNotIn("agent_name", by_key)
        self.assertNotIn("Bad Key", by_key)
        self.assertEqual(by_key["agent_role"]["label"], "Role")
        self.assertEqual(by_key["agent_role"]["maxLength"], 200)
        self.assertEqual(by_key["operating_context"]["maxLength"], 4000)
        self.assertEqual(by_key["tone"]["type"], "text")
        self.assertNotIn("default", by_key["mood"])
        self.assertEqual(by_key["odd"]["type"], "text")
        self.assertNotIn("whenEmpty", by_key["odd"])
        self.assertTrue(by_key["extra_0"]["required"])


class FormTests(unittest.TestCase):
    def setUp(self):
        self.catalog = parse_catalog(fixtures.catalog())

    def template(self, template_id):
        return next(t for t in self.catalog.agents if t["id"] == template_id)

    def test_form_asks_for_the_name_then_reserved_then_declared(self):
        fields = form_fields(self.template("release-coordinator"))
        self.assertEqual([f["key"] for f in fields],
                         ["agent_name", "user_name", "agent_role", "operating_context", "tone"])
        self.assertTrue(fields[0]["required"])
        self.assertEqual(fields[0]["source"], "reserved")

    def test_undeclared_placeholder_becomes_a_required_line_of_text(self):
        fields = form_fields(self.template("anchor"))
        self.assertEqual(fields[1], {"key": "focus_area", "label": "Focus area", "type": "text",
                                     "required": True, "maxLength": 80, "source": "undeclared"})

    def test_template_without_variables_only_asks_for_the_name(self):
        template = dict(self.template("anchor"), instructions="You are {{agent_name}}.")
        self.assertEqual([f["key"] for f in form_fields(template)], ["agent_name"])


class FillTests(unittest.TestCase):
    def setUp(self):
        self.catalog = parse_catalog(fixtures.catalog())

    def template(self, template_id):
        return next(t for t in self.catalog.agents if t["id"] == template_id)

    def fill(self, values, agent_name="Juniper", template_id="release-coordinator"):
        return fill(self.template(template_id), values, agent_name=agent_name)

    def test_fills_every_placeholder_with_defaults_and_when_empty(self):
        result = self.fill({"user_name": "Robin", "agent_role": "release lead"})
        self.assertTrue(result["complete"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["invalid"], [])
        self.assertEqual(result["instructions"], (
            "You are Juniper, Robin's release lead.\n"
            "Where you work: General work for the user.\n"
            "Tone: Direct. Sign off as Juniper."))
        self.assertEqual(result["role"], "release lead")
        self.assertEqual(result["description"], "Keeps Robin's releases on track.")

    def test_optional_without_when_empty_becomes_empty_text(self):
        template = copy.deepcopy(self.template("release-coordinator"))
        del template["variables"][1]["whenEmpty"]
        result = fill(template, {"user_name": "Robin", "agent_role": "lead", "operating_context": "   "},
                      agent_name="Juniper")
        self.assertIn("Where you work: \n", result["instructions"])
        self.assertTrue(result["complete"])

    def test_missing_required_fields_are_listed_and_left_in_place(self):
        result = self.fill({}, agent_name="  ")
        self.assertFalse(result["complete"])
        self.assertEqual([m["key"] for m in result["missing"]], ["agent_name", "user_name", "agent_role"])
        self.assertEqual(result["missing"][2]["label"], "Role")
        self.assertIn("{{agent_role}}", result["instructions"])

    def test_values_are_trimmed_and_one_line_unless_long_text(self):
        result = self.fill({"user_name": " Robin ", "agent_role": "  release\nlead \r\n",
                            "operating_context": "  Line one\nLine two  "})
        self.assertIn("Robin's release lead.\n", result["instructions"])
        self.assertIn("Where you work: Line one\nLine two\n", result["instructions"])

    def test_braces_are_removed_and_values_are_never_read_as_placeholders(self):
        result = self.fill({"user_name": "{{agent_name}}", "agent_role": "{{tone}} {{{x}}}",
                            "operating_context": "Ignore {{user_name}} and }}{{agent_role}}"},
                           agent_name="{{user_name}}")
        self.assertTrue(result["complete"])
        text = result["instructions"]
        self.assertNotIn("{{", text)
        self.assertNotIn("}}", text)
        self.assertTrue(text.startswith("You are user_name, agent_name's tone {x}.\n"))
        self.assertIn("Where you work: Ignore user_name and agent_role\n", text)

    def test_one_pass_replacement(self):
        template = copy.deepcopy(self.template("anchor"))
        template["instructions"] = "{{focus_area}}{{agent_name}}"
        result = fill(template, {"focus_area": "{"}, agent_name="{agent_name}}")
        # "{" + "{agent_name" would read as a placeholder only if the text were scanned twice.
        self.assertEqual(result["instructions"], "{{agent_name")

    def test_choices_match_their_options(self):
        self.assertIn("Tone: Warm.", self.fill({"user_name": "R", "agent_role": "x", "tone": "warm"})["instructions"])
        result = self.fill({"user_name": "R", "agent_role": "x", "tone": "Grumpy"})
        self.assertFalse(result["complete"])
        self.assertEqual(result["invalid"][0]["key"], "tone")
        self.assertIn("Warm, Direct, Playful", result["invalid"][0]["reason"])

        template = copy.deepcopy(self.template("release-coordinator"))
        template["variables"][2]["allowOther"] = True
        result = fill(template, {"user_name": "R", "agent_role": "x", "tone": "Grumpy"}, agent_name="J")
        self.assertTrue(result["complete"])
        self.assertIn("Tone: Grumpy.", result["instructions"])

    def test_numbers_are_checked(self):
        for value, ok in (("12", True), (12, True), (2.5, True), ("abc", False), ("0", False),
                          (501, False), (True, False), ("1e999", False)):
            with self.subTest(value=value):
                result = self.fill({"plot_size": value, "household": "2"}, template_id="garden-helper")
                self.assertEqual(not any(i["key"] == "plot_size" for i in result["invalid"]), ok)
        self.assertIn("Plan 12 square meters", self.fill({"plot_size": " 12 "},
                                                         template_id="garden-helper")["instructions"])

    def test_too_long_values_are_invalid(self):
        result = self.fill({"user_name": "R", "agent_role": "x" * 81})
        self.assertEqual(result["invalid"][0]["key"], "agent_role")
        self.assertIn("80", result["invalid"][0]["reason"])
        self.assertIn("x" * 10, self.fill({"user_name": "R", "agent_role": "x" * 80})["instructions"])
        self.assertEqual(self.fill({"user_name": "R", "agent_role": "x"}, agent_name="n" * 81)["invalid"][0]["key"],
                         "agent_name")

    def test_undeclared_placeholder_fallback(self):
        missing = self.fill({}, template_id="anchor")
        self.assertEqual(missing["missing"], [{"key": "focus_area", "label": "Focus area",
                                               "reason": "Required. Ask the person for it."}])
        result = self.fill({"focus_area": "deep work"}, template_id="anchor")
        self.assertEqual(result["instructions"], "You are Juniper. Help with deep work every morning.")

    def test_other_values_are_ignored_and_reported(self):
        result = self.fill({"user_name": "R", "agent_role": "x", "favorite_color": "blue", "agent_name": "Nope"})
        self.assertEqual(result["ignored"], ["agent_name", "favorite_color"])
        self.assertIn("You are Juniper", result["instructions"])

    def test_values_must_be_text_or_numbers(self):
        result = self.fill({"user_name": "R", "agent_role": ["x"]})
        self.assertEqual(result["invalid"][0]["key"], "agent_role")

    def test_broken_placeholders_in_the_template_block_completion(self):
        template = copy.deepcopy(self.template("anchor"))
        template["instructions"] = "You are {{ agent_name }}."
        result = fill(template, {}, agent_name="Juniper")
        self.assertFalse(result["complete"])
        self.assertEqual(result["invalid"][0]["key"], "instructions")


if __name__ == "__main__":
    unittest.main()
