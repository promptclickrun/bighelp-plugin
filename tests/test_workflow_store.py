from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import stat
import sys
import threading
import unittest

from loopdy_plugin import workflow_model as model
from loopdy_plugin import workflow_store
from loopdy_plugin.workflow_store import Mutation, StoreUnavailable, WorkflowError, WorkflowStore
from workflow_fixtures import DRAFT, Engine, INPUTS


CLIENT_RUN = "6f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b"


def request_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


class DefinitionStoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        self.store = self.engine.store

    def error(self, call, *args, **kwargs) -> WorkflowError:
        with self.assertRaises(WorkflowError) as caught:
            call(*args, **kwargs)
        return caught.exception

    def test_drafts_are_versioned_and_conflicts_refused(self):
        definition = model.template("research-draft-review")
        created = self.store.save_draft(None, 0, definition, self.engine.facts)
        self.assertEqual(created["draftVersion"], 1)
        self.assertRegex(created["workflowId"], r"^wf_[0-9a-f]{16}$")
        self.assertFalse(created["validation"]["host"])
        definition["name"] = "Renamed"
        self.assertEqual(self.store.save_draft(created["workflowId"], 1, definition, None)["draftVersion"], 2)
        self.assertEqual(self.error(self.store.save_draft, created["workflowId"], 1, definition, None).code,
                         "draft_conflict")
        self.assertEqual(self.error(self.store.save_draft, None, 3, definition, None).code, "draft_conflict")
        self.assertEqual(self.error(self.store.save_draft, None, 0, {"schemaVersion": 1}, None).code,
                         "invalid_definition")
        huge = dict(definition, stages=[dict(definition["stages"][0], key=f"s{index}", instructions="x" * 8000)
                                        for index in range(10)])
        self.assertEqual(self.error(self.store.save_draft, None, 0, huge, None).status, 413)
        got = self.store.get_workflow(created["workflowId"], "draft", self.engine.facts)["workflow"]
        self.assertEqual((got["name"], got["draftVersion"], got["revision"], got["latestRevision"]),
                         ("Renamed", 2, None, None))
        self.assertEqual([item["agentId"] for item in got["bindings"]], [None, None, None])

    def test_publish_needs_a_valid_draft_and_reuses_identical_revisions(self):
        workflow_id, revision = self.engine.workflow(bind=False)
        self.assertEqual(revision, 1)
        self.assertEqual(self.store.publish(workflow_id, 1, None)["revision"], 1)
        self.assertEqual(self.error(self.store.publish, workflow_id, 2, None).code, "draft_conflict")
        broken = model.template("research-draft-review")
        broken["stages"][1]["uses"].append("draft.nothing")
        self.store.save_draft(workflow_id, 1, broken, None)
        self.assertEqual(self.error(self.store.publish, workflow_id, 2, None).code, "not_valid")
        fixed = model.template("research-draft-review")
        fixed["description"] = "Second version."
        self.store.save_draft(workflow_id, 2, fixed, None)
        self.assertEqual(self.store.publish(workflow_id, 3, None)["revision"], 2)
        self.assertEqual(self.store.get_workflow(workflow_id, 1, None)["workflow"]["definition"]["description"],
                         model.RESEARCH_DRAFT_REVIEW["description"])
        self.assertEqual(self.error(self.store.get_workflow, workflow_id, 9, None).code, "revision_not_found")

    def test_bindings_roles_archive_and_list(self):
        workflow_id, _ = self.engine.workflow(bind=False)
        self.assertEqual(self.error(self.store.bind, workflow_id, "pilot", "writer").code, "role_not_found")
        bindings = self.store.bind(workflow_id, "writer", "writer")["bindings"]
        self.assertEqual([item["agentId"] for item in bindings], [None, "writer", None])
        self.assertRegex(bindings[1]["approvedAt"], r"Z$")
        listed = self.store.list_workflows(False, self.engine.facts)["workflows"][0]
        self.assertEqual(listed["needsSetupRoles"], ["researcher", "reviewer"])
        self.assertEqual((listed["revision"], listed["hasDraft"], listed["stageCount"], listed["valid"]),
                         (1, False, 6, True))
        self.store.bind(workflow_id, "writer", None)
        self.assertEqual(self.store.list_workflows(False, None)["workflows"][0]["needsSetupRoles"],
                         ["researcher", "writer", "reviewer"])
        self.assertEqual(self.store.archive(workflow_id), {"workflowId": workflow_id, "archived": True})
        self.assertEqual(self.store.list_workflows(False, None)["workflows"], [])
        self.assertTrue(self.store.list_workflows(True, None)["workflows"][0]["archived"])
        self.assertEqual(self.error(self.store.bind, workflow_id, "writer", "writer").code, "workflow_archived")
        self.assertEqual(self.error(self.store.get_workflow, "wf_0000000000000000", "draft", None).code,
                         "workflow_not_found")

    def test_mutations_replay_by_request_id_and_refuse_reuse(self):
        first = self.store.use_template("research-draft-review", None, None, Mutation(request_id(1)))
        again = self.store.use_template("research-draft-review", None, None, Mutation(request_id(1)))
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.list_workflows(False, None)["workflows"]), 1)
        self.assertEqual(self.error(self.store.archive, first["workflowId"], Mutation(request_id(1))).code,
                         "request_reused")
        self.assertEqual(self.error(self.store.use_template, "nope", None, None).code, "template_not_found")

    def test_failed_context_check_rolls_the_change_back(self):
        workflow_id, _ = self.engine.workflow(bind=False)

        def changed():
            raise RuntimeError("context changed")

        with self.assertRaises(RuntimeError):
            self.store.bind(workflow_id, "writer", "writer", Mutation(request_id(2), check=changed))
        self.assertEqual([item["agentId"] for item in self.store.get_workflow(
            workflow_id, "draft", None)["workflow"]["bindings"]], [None, None, None])
        self.assertEqual(self.store.bind(workflow_id, "writer", "writer", Mutation(request_id(2)))["bindings"][1][
            "agentId"], "writer")


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        self.store = self.engine.store
        self.workflow_id, self.revision = self.engine.workflow()

    def error(self, call, *args, **kwargs) -> WorkflowError:
        with self.assertRaises(WorkflowError) as caught:
            call(*args, **kwargs)
        return caught.exception

    def test_start_is_idempotent_by_client_token_and_checked(self):
        first = self.store.start_run(self.workflow_id, 1, INPUTS, CLIENT_RUN, False, self.engine.facts)["run"]
        again = self.store.start_run(self.workflow_id, 1, INPUTS, CLIENT_RUN, False, self.engine.facts)["run"]
        self.assertEqual(first, again)
        self.assertEqual((first["state"], first["number"], first["stageKey"], first["stageState"]),
                         ("planned", 1, "research", "pending"))
        self.assertEqual(self.error(self.store.start_run, self.workflow_id, 1, {"length": "Short"}, request_id(3),
                                    False, self.engine.facts).code, "inputs_invalid")
        self.assertEqual(self.error(self.store.start_run, self.workflow_id, 1, INPUTS, request_id(4), True,
                                    self.engine.facts).code, "sample_unsupported")
        self.assertEqual(self.error(self.store.start_run, self.workflow_id, 7, INPUTS, request_id(5), False,
                                    self.engine.facts).code, "revision_not_found")
        self.store.bind(self.workflow_id, "writer", None)
        self.assertEqual(self.error(self.store.start_run, self.workflow_id, 1, INPUTS, request_id(6), False,
                                    self.engine.facts).code, "not_valid")

    def test_queue_is_bounded(self):
        for number in range(20):
            self.store.start_run(self.workflow_id, 1, INPUTS, request_id(100 + number), False, self.engine.facts)
        error = self.error(self.store.start_run, self.workflow_id, 1, INPUTS, request_id(200), False,
                           self.engine.facts)
        self.assertEqual((error.status, error.code), (429, "queue_full"))

    def test_list_filters_and_pages(self):
        ids = [self.store.start_run(self.workflow_id, 1, INPUTS, request_id(300 + number), False,
                                    self.engine.facts)["run"]["id"] for number in range(5)]
        page = self.store.list_runs(None, "all", None, 2)
        self.assertEqual([run["id"] for run in page["runs"]], list(reversed(ids))[:2])
        self.assertTrue(page["hasMore"])
        rest = self.store.list_runs(self.workflow_id, "all", page["cursor"], 50)
        self.assertEqual([run["id"] for run in rest["runs"]], list(reversed(ids))[2:])
        self.assertFalse(rest["hasMore"])
        self.assertEqual(len(self.store.list_runs(None, "active", None, 50)["runs"]), 5)
        self.assertEqual(self.store.list_runs(None, "for_you", None, 50)["runs"], [])
        version = self.store.get_run(ids[0])["run"]["version"]
        self.assertEqual(self.error(self.store.control, ids[0], "cancel", version + 1).code, "run_conflict")
        self.assertEqual(self.error(self.store.control, ids[0], "retry", version).code, "not_allowed")
        cancelled = self.store.control(ids[0], "cancel", version)["run"]
        self.assertEqual((cancelled["state"], cancelled["version"]), ("cancelled", version + 1))
        self.assertEqual(self.store.get_run(ids[0])["run"]["allowedActions"], [])
        self.assertEqual(self.error(self.store.get_run, "run_0000000000000000").code, "run_not_found")

    def waiting_run(self):
        self.engine.start()
        run_id = self.engine.run(self.workflow_id, self.revision)
        self.engine.through_review(run_id)
        detail = self.engine.detail(run_id)
        self.assertEqual(detail["state"], "waiting_for_you")
        return run_id, detail

    def test_artifacts_are_read_in_chunks_and_only_from_their_run(self):
        run_id, detail = self.waiting_run()
        digest = detail["signoff"]["artifact"]["sha256"]
        data, offset = b"", 0
        while True:
            chunk = self.store.read_artifact(run_id, digest, offset, 100)
            data += base64.b64decode(chunk["data"])
            offset += 100
            if chunk["done"]:
                break
        self.assertEqual(data, DRAFT.encode("utf-8"))
        self.assertEqual(chunk["total"], len(DRAFT.encode("utf-8")))
        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        other = self.store.start_run(self.workflow_id, 1, INPUTS, request_id(400), False, self.engine.facts)["run"]
        self.assertEqual(self.error(self.store.read_artifact, other["id"], digest, 0, 10).code, "artifact_not_found")
        self.assertEqual(self.error(self.store.read_artifact, run_id, "0" * 64, 0, 10).code, "artifact_not_found")
        info = os.stat(self.store.artifacts / digest)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o400)

    def test_signoff_is_bound_to_the_exact_file(self):
        run_id, detail = self.waiting_run()
        digest = detail["signoff"]["artifact"]["sha256"]
        self.assertEqual(self.error(self.store.signoff, run_id, "signoff", "approve", "f" * 64, "").code,
                         "approval_stale")
        self.assertEqual(self.error(self.store.signoff, run_id, "review", "approve", digest, "").code, "not_waiting")
        path = self.store.artifacts / digest
        os.chmod(path, 0o600)
        path.write_bytes(b"# Swapped\n")
        self.assertEqual(self.error(self.store.signoff, run_id, "signoff", "approve", digest, "").code,
                         "approval_stale")
        path.write_bytes(DRAFT.encode("utf-8"))
        run = self.store.signoff(run_id, "signoff", "approve", digest, "")["run"]
        self.assertEqual((run["state"], run["stagesDone"]), ("succeeded", 6))
        self.assertEqual(self.error(self.store.signoff, run_id, "signoff", "approve", digest, "").code, "not_waiting")


class HygieneTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)

    def test_folders_are_owner_only_and_links_refused(self):
        self.engine.workflow(bind=False)
        for folder in (self.engine.root, self.engine.store.artifacts, self.engine.store.runs_dir):
            self.assertEqual(stat.S_IMODE(os.lstat(folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.engine.store.database).st_mode), 0o600)
        linked = self.engine.home / "linked"
        linked.symlink_to(self.engine.root)
        with self.assertRaises(StoreUnavailable):
            WorkflowStore(linked).status()
        os.link(self.engine.store.database, self.engine.home / "copy.sqlite3")
        with self.assertRaises(StoreUnavailable):
            self.engine.store.status()

    def test_a_foreign_database_is_refused(self):
        import sqlite3
        root = self.engine.home / "foreign"
        root.mkdir(mode=0o700)
        connection = sqlite3.connect(root / "workflows.sqlite3")
        connection.execute("CREATE TABLE secrets (value TEXT)")
        connection.commit()
        connection.close()
        with self.assertRaises(StoreUnavailable):
            WorkflowStore(root).status()

    def test_both_module_copies_write_one_database(self):
        """The dashboard loads `loopdy_plugin` and `hermes_plugins.loopdy.loopdy_plugin` side by side."""
        package = Path(workflow_store.__file__).resolve().parent
        name = "hermes_plugins_fixture_copy"
        spec = importlib.util.spec_from_file_location(name, package / "__init__.py",
                                                      submodule_search_locations=[str(package)])
        copy = importlib.util.module_from_spec(spec)
        sys.modules[name] = copy
        self.addCleanup(lambda: [sys.modules.pop(key) for key in list(sys.modules) if key.startswith(name)])
        spec.loader.exec_module(copy)
        second = importlib.import_module(name + ".workflow_store")
        self.assertIsNot(second, workflow_store)
        stores = [workflow_store.WorkflowStore(self.engine.root), second.WorkflowStore(self.engine.root)]
        errors: list[BaseException] = []

        def write(store, offset):
            try:
                for number in range(15):
                    store.use_template("research-draft-review", f"Copy {offset + number}", None)
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=write, args=(store, index * 100)) for index, store in enumerate(stores)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(stores[0].list_workflows(False, None)["workflows"]), 30)
        self.assertEqual(len(stores[1].list_workflows(False, None)["workflows"]), 30)



if __name__ == "__main__":
    unittest.main()
