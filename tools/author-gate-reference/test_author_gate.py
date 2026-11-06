"""作者启动门禁参考判定的离线测试：只用虚构向量与合成快照，不调用模型、不联网、不读写真实状态。"""

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import author_gate as gate


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
VECTORS = ROOT / "examples/author-gate/vectors.json"
CONTRACT = ROOT / "routing/AUTHOR-GATE.md"
CODE = re.compile(r"(?:AUTHOR|PROBE)-[A-Z0-9]+(?:-[A-Z0-9]+)*")
# Codes only a real wrapper can raise: the reference never runs inside a sandbox.
EXECUTOR_ONLY = {"AUTHOR-PERMIT-MISSING"}


def vectors():
    return gate._strict_json(VECTORS.read_text(encoding="utf-8"))


def policy(**changes):
    return {**vectors()["policy"], **changes}


def request(**fields):
    return {"engine": "engine-alpha", "role": "code", "work_id": "W-A", "ack": "W-A:engine-alpha:code", **fields}


def beta_wrote(high=True):
    registry = {"high_risk": ["W-A"]} if high else {}
    return {"registry": registry, "runs": [{"work_id": "W-A", "engine": "engine-beta", "role": "code"}]}


class VectorTests(unittest.TestCase):
    def test_every_vector_matches_the_reference(self):
        outcomes = gate.run_document(vectors())
        self.assertGreaterEqual(len(outcomes), 90)
        for label, ok, mismatches in outcomes:
            with self.subTest(vector=label):
                self.assertTrue(ok, mismatches)

    def test_vectors_cover_every_contract_topic(self):
        document = vectors()
        covers = {tag for item in document["vectors"] + document["sequences"] for tag in item["covers"]}
        self.assertLessEqual({"writer-lock", "second-family", "dual-existing", "review", "serialization",
                              "probe-identity", "probe-isolation", "probe-purpose", "fail-closed", "exception",
                              "permit", "family", "linked-set", "risk", "ack"}, covers)

    def test_contract_and_vectors_name_the_same_codes(self):
        documented = set(CODE.findall(CONTRACT.read_text(encoding="utf-8")))
        text = VECTORS.read_text(encoding="utf-8")
        expected = set(CODE.findall(text))
        unit_only = {"AUTHOR-SOURCE-REGISTRY-FORMAT", "AUTHOR-SOURCE-LEDGER-FORMAT"}
        self.assertLessEqual(expected, documented)
        self.assertEqual(documented - expected - unit_only - EXECUTOR_ONLY, set())
        self.assertLessEqual(set(gate.MISSING_APPROVAL), documented)

    def test_vectors_use_fictitious_names_only(self):
        document = vectors()
        self.assertTrue(all(f.startswith("engine-") for f in document["policy"]["families"]))
        requests = [v["request"] for v in document["vectors"]]
        requests += [s["request"] for q in document["sequences"] for s in q["steps"] if "request" in s]
        for item in requests:
            with self.subTest(work_id=item["work_id"]):
                self.assertRegex(item["work_id"], r"^W-[A-Z0-9-]+$")
                self.assertTrue(item["engine"].startswith("engine-"))

    def test_a_wrong_expectation_is_reported_by_key_name(self):
        document = vectors()
        document["vectors"][0]["expect"]["codes"] = ["AUTHOR-LOCK-MISMATCH"]
        label, ok, mismatches = gate.run_document(document)[0]
        self.assertEqual((label, ok, mismatches), (document["vectors"][0]["id"], False, ["codes"]))


class DecisionTests(unittest.TestCase):
    def test_inputs_are_never_modified_and_the_result_is_deterministic(self):
        snapshot, req, pol = beta_wrote(), request(), policy()
        before = copy.deepcopy((snapshot, req, pol))
        first = gate.evaluate(req, snapshot, pol)
        self.assertEqual(gate.evaluate(req, snapshot, pol), first)
        gate.issue_permit(req, snapshot, pol, "p-1")
        self.assertEqual((snapshot, req, pol), before)

    def test_ack_never_unlocks_and_comes_after_the_hard_decision(self):
        for ack in ("W-A:engine-alpha:code", None, "W-A:engine-beta:code"):
            with self.subTest(ack=ack):
                result = gate.evaluate(request(ack=ack), beta_wrote(), policy())
                self.assertEqual(result["codes"], ["AUTHOR-LOCK-MISMATCH", "AUTHOR-LAUNCH-SECOND-FAMILY"])
        result = gate.evaluate(request(ack="W-A:engine-beta:code"), {}, policy())
        self.assertEqual((result["code"], result["reason"]), ("AUTHOR-LAUNCH-DENIED", "ack"))
        self.assertTrue(gate.evaluate(request(ack=None), {}, policy(ack_required=False))["allowed"])

    def test_source_structure_errors_refuse(self):
        cases = [({"registry": []}, "AUTHOR-SOURCE-REGISTRY-FORMAT"),
                 ({"registry": {"parents": []}}, "AUTHOR-SOURCE-REGISTRY-FORMAT"),
                 ({"registry": {"unknown": {}}}, "AUTHOR-SOURCE-REGISTRY-FORMAT"),
                 ({"runs": {}}, "AUTHOR-SOURCE-LEDGER-FORMAT"),
                 ({"runs": ["broken"]}, "AUTHOR-SOURCE-LEDGER-FORMAT"),
                 ({"permits": {}}, "AUTHOR-PERMIT-FORMAT"),
                 ({"permits": ["broken"]}, "AUTHOR-PERMIT-FORMAT"),
                 ({"unknown": []}, "AUTHOR-LAUNCH-DENIED")]
        for snapshot, code in cases:
            with self.subTest(snapshot=snapshot):
                result = gate.evaluate(request(), snapshot, policy())
                self.assertEqual((result["allowed"], result["codes"]), (False, [code]))

    def test_unhashable_or_odd_values_fail_closed_without_raising(self):
        cases = [
            ({"runs": [{"work_id": "W-A", "engine": ["engine-beta"], "role": "code"}]}, "AUTHOR-SOURCE-LEDGER-ENTRY"),
            ({"runs": [{"work_id": "W-A", "engine": "engine-beta", "role": "code",
                        "actual_engine_families": [["engine-beta"]]}]}, "AUTHOR-SOURCE-LEDGER-ENTRY"),
            ({"runs": [{"work_id": ["W-A"], "engine": "engine-beta"}]}, None),
            ({"permits": [{"type": "permit", "permit_id": "p", "work_id": "W-A", "family": ["x"], "role": "code",
                           "exception_id": None}]}, "AUTHOR-PERMIT-FORMAT"),
            ({"permits": [{"type": "permit", "work_id": ["W-A"]}]}, None),
            ({"registry": {"writer_locks": {"W-A": ["engine-alpha"]}}}, "AUTHOR-SOURCE-REGISTRY-ENTRY"),
            ({"registry": {"authors": {"W-A": [["engine-beta"]]}}}, "AUTHOR-SOURCE-REGISTRY-ENTRY"),
            ({"registry": {"high_risk": [["W-A"]]}}, "AUTHOR-SOURCE-REGISTRY-FORMAT"),
            ({"unavailable": [["runs"]]}, "AUTHOR-LAUNCH-DENIED"),
            ({"adhoc": {"W-P": ["D1"]}}, "AUTHOR-LAUNCH-DENIED")]
        for snapshot, code in cases:
            with self.subTest(snapshot=snapshot):
                result = gate.evaluate(request(), snapshot, policy())
                if code is None:
                    self.assertTrue(result["allowed"], result)
                    self.assertTrue(result["warnings"])
                else:
                    self.assertEqual((result["allowed"], result["codes"]), (False, [code]))
        for odd in ({"model_families": {"m": ["engine-alpha"]}}, {"high_risk_pair": [["a"], ["b"]]},
                    {"reviewers": {"engine-alpha": ["engine-alpha"]}}, {"probe": {}}, {"ack_required": 1}):
            with self.subTest(policy=odd):
                self.assertEqual(gate.evaluate(request(), {}, policy(**odd))["codes"], ["AUTHOR-POLICY"])
        for odd in ([], {"engine": "engine-alpha"}, request(engine="engine-delta"), request(role="writer"),
                    request(work_id="W A"), request(probe="yes"), request(cwd="somewhere"), request(model=7)):
            with self.subTest(request=odd):
                self.assertEqual(gate.evaluate(odd, {}, policy())["codes"], ["AUTHOR-LAUNCH-DENIED"])

    def test_malformed_approvals_are_one_format_refusal(self):
        approval = copy.deepcopy(vectors()["sequences"][-1]["snapshot"]["exceptions"]["approvals"][0])
        for field, value in (("acknowledges", [1, "second_family_becomes_author"]), ("max_uses", 2),
                             ("existing_families", ["engine-beta", "engine-beta"]), ("purpose", "probe"),
                             ("not_before", "2026-01-01T00:00:00"), ("lock_family", "human"), ("role", "review"),
                             ("approval_source", "line\nbreak"), ("input_sha256", "*")):
            with self.subTest(field=field):
                document = {"version": 1, "approvals": [{**approval, field: value}], "revoked": []}
                result = gate.evaluate(request(exception_id="EXC-W-A-1", now="2026-01-02T00:00:00+00:00"),
                                       {**beta_wrote(), "exceptions": document}, policy())
                self.assertEqual(result["code"], "AUTHOR-EXCEPTION-FORMAT")
        result = gate.evaluate(request(exception_id="bad id"), beta_wrote(), policy())
        self.assertEqual(result["code"], "AUTHOR-EXCEPTION-FORMAT")

    def test_exception_time_must_be_provable(self):
        snapshot = copy.deepcopy(vectors()["sequences"][-1]["snapshot"])
        for now in (None, "2026-01-02", "2026-01-03T00:00:00+00:00"):
            with self.subTest(now=now):
                result = gate.evaluate(request(exception_id="EXC-W-A-1", now=now), snapshot, policy())
                self.assertEqual(result["code"], "AUTHOR-EXCEPTION-EXPIRED")

    def test_unknown_stage_is_a_caller_error(self):
        with self.assertRaises(ValueError):
            gate.evaluate(request(), {}, policy(), stage="late")


class RecordTests(unittest.TestCase):
    def test_permit_record_fields_and_canonical_digest(self):
        out = gate.issue_permit(request(run_id="run-1", model="alpha-code", auth_resolved="pool-a"), {}, policy(),
                                "p-1", "2026-01-02T00:00:00+00:00", "host")
        permit = out["permit"]
        self.assertEqual(tuple(permit), gate.PERMIT_FIELDS)
        canonical = json.dumps(permit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(out["permit_sha256"], hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual((permit["policy"], permit["entry"], permit["auth_resolved"], permit["model"]),
                         ("author-launch/v2", "host", "pool-a", "alpha-code"))
        self.assertNotEqual(gate.permit_sha256({**permit, "run_id": "run-2"}), out["permit_sha256"])
        self.assertIsNone(out["refusal"])

    def test_refusal_record_fields_carry_no_prompt_or_label(self):
        req = request(run_id="run-9", task="W-A synthetic label", input_sha256="ab" * 32)
        out = gate.issue_permit(req, beta_wrote(), policy(), "p-9")
        refusal = out["refusal"]
        self.assertIsNone(out["permit"])
        self.assertEqual(tuple(refusal), gate.REFUSAL_FIELDS)
        self.assertEqual((refusal["code"], refusal["codes"], refusal["run_id"], refusal["entry"]),
                         ("AUTHOR-LOCK-MISMATCH", ["AUTHOR-LOCK-MISMATCH", "AUTHOR-LAUNCH-SECOND-FAMILY"],
                          "run-9", "host"))
        self.assertEqual(refusal["lock_before"], {"state": "locked", "family": "engine-beta"})
        text = json.dumps(refusal, ensure_ascii=False)
        self.assertNotIn("synthetic label", text)
        self.assertNotIn("ab" * 32, text)

    def test_an_audit_write_failure_still_refuses(self):
        out = gate.issue_permit(request(), {**beta_wrote(), "unavailable": ["refusal_audit"]}, policy(), "p-1")
        self.assertEqual((out["result"]["allowed"], out["audit_written"], out["permit"]), (False, False, None))
        self.assertEqual(out["result"]["codes"], ["AUTHOR-LOCK-MISMATCH", "AUTHOR-LAUNCH-SECOND-FAMILY"])

    def test_early_refusal_record_before_any_source_is_read(self):
        req = request(purpose="probe")
        result = gate.evaluate(req, {}, policy())
        refusal = gate.refusal_record(req, result, "2026-01-02T00:00:00+00:00", "early")
        self.assertEqual((refusal["code"], refusal["linked_ids"], refusal["lock_before"]),
                         ("PROBE-PURPOSE-CONFLICT", None, None))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        out = gate.issue_permit(request(run_id="run-1", model="alpha-code"), {}, policy(), "p-1")
        self.permit, digest = out["permit"], out["permit_sha256"]
        self.row = {"run_id": "run-1", "work_id": "W-A", "engine": "engine-alpha", "role": "code",
                    "author_launch_policy": "author-launch/v2", "launch_permit_id": "p-1",
                    "launch_permit_sha256": digest, "actual_engine_families": ["engine-alpha"]}

    def evidence(self, row=None, rows=None, records=None, **options):
        row = self.row if row is None else row
        return gate.permit_evidence(row, [row] if rows is None else rows,
                                    [self.permit] if records is None else records, **options)

    def test_verified_and_each_failure(self):
        self.assertEqual(self.evidence(), ("verified", []))
        void = {"type": "void", "permit_id": "p-1", "work_id": "W-A", "reason": "never_started"}
        cases = [(dict(self.row, launch_permit_sha256="0" * 64), None, None, "launch permit digest mismatch"),
                 (dict(self.row, engine="engine-beta", actual_engine_families=["engine-beta"]), None, None,
                  "launch permit identity mismatch"),
                 (dict(self.row, run_id="run-2"), None, None, "launch permit run mismatch"),
                 (None, None, [self.permit, void], "launch permit voided but the run is recorded"),
                 (dict(self.row, actual_engine_families=["engine-alpha", "engine-beta"]), None, None,
                  "actual engine families differ from the launch permit"),
                 (None, [self.row, dict(self.row, run_id="run-2")], None, "launch permit named by several runs")]
        for row, rows, records, error in cases:
            with self.subTest(error=error):
                status, errors = self.evidence(row, rows, records)
                self.assertEqual(status, "invalid")
                self.assertIn(error, errors)

    def test_missing_unique_legacy_and_isolated_outer_run(self):
        self.assertEqual(self.evidence(records=[])[0], "missing")
        self.assertEqual(self.evidence(dict(self.row, launch_permit_id=None))[0], "missing")
        self.assertEqual(self.evidence(records=[self.permit, dict(self.permit)])[0], "invalid")
        self.assertEqual(self.evidence(records=["broken"])[0], "invalid")
        legacy = {key: value for key, value in self.row.items() if key != "author_launch_policy"}
        self.assertEqual(self.evidence(legacy), ("legacy", []))
        self.assertEqual(self.evidence(legacy, require=True)[0], "legacy")
        self.assertTrue(self.evidence(legacy, require=True)[1])
        inner = dict(self.row, run_id="run-inner")
        self.assertEqual(self.evidence(inner)[0], "invalid")
        self.assertEqual(self.evidence(inner, outer_run_id="run-1"), ("verified", []))


class BoundaryTests(unittest.TestCase):
    def test_the_reference_imports_only_pure_standard_modules(self):
        tree = ast.parse((HERE / "author_gate.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertLessEqual(names, {"copy", "datetime", "hashlib", "json", "re", "sys", "unicodedata"})

    def run_cli(self, *args):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-B", str(HERE / "author_gate.py"), *args],
                              text=True, capture_output=True, env=env)

    def test_cli_checks_every_vector_and_reports_mismatches(self):
        result = self.run_cli(str(VECTORS))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(line.startswith("不符 ") for line in result.stdout.splitlines()))
        self.assertIn("不符 0 条", result.stdout)
        with tempfile.TemporaryDirectory(prefix="author-gate-") as temp:
            document = vectors()
            document["vectors"][0]["expect"]["allowed"] = False
            wrong = Path(temp) / "wrong.json"
            wrong.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            result = self.run_cli(str(wrong))
            self.assertEqual(result.returncode, 1)
            self.assertIn("不符 " + document["vectors"][0]["id"] + "：allowed", result.stdout)
            broken = Path(temp) / "broken.json"
            broken.write_text('{"format": "author-gate-vectors/1", "format": 1}', encoding="utf-8")
            result = self.run_cli(str(broken))
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("format", result.stderr)
        self.assertEqual(self.run_cli().returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
