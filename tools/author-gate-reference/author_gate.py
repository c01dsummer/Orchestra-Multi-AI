#!/usr/bin/env python3
"""作者启动门禁契约 author-launch/v2 的标准库参考判定：纯计算，不是执行器，也不是强制门禁。

本模块只按调用方给出的 policy、snapshot、request 计算启动判定、许可记录、拒绝审计记录与交付证据，
不读写来源、不加锁、不落盘、不调用模型或网络。命令行入口只读取指定的虚构向量文件并逐条核对。
强制点在使用者自备的私有执行器：由它读取来源、持锁重算并落盘，语义见 routing/AUTHOR-GATE.md。
"""

import copy
import datetime as dt
import hashlib
import json
import re
import sys
import unicodedata


POLICY = "author-launch/v2"
VECTORS_FORMAT = "author-gate-vectors/1"
HUMAN = "human"
ROLES = ("code", "other", "review")
PURPOSES = ("probe", "status", "work")
NONE, LOCKED, CONFLICT, UNKNOWN = "none", "locked", "conflict", "unknown"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
DENIED = "AUTHOR-LAUNCH-DENIED"
# Primary code -> the approval a person would have to give (None: no approval can lift it).
MISSING_APPROVAL = {
    "AUTHOR-FAMILY-PREFLIGHT": None, "AUTHOR-FAMILY-UNPROVABLE": None,
    "AUTHOR-LOCK-CONFLICT": "writer_lock_reconciliation", "AUTHOR-LOCK-UNKNOWN": "writer_lock_registration",
    "AUTHOR-LOCK-MISMATCH": "lock_exception", "AUTHOR-LAUNCH-DUAL-EXISTING": "second_family_exception",
    "AUTHOR-LAUNCH-SECOND-FAMILY": "second_family_exception"}
# What the wrapper could not read or take; each refuses with its code (an audit write failure still refuses).
UNAVAILABLE = {"registry": "AUTHOR-SOURCE-REGISTRY-READ", "runs": "AUTHOR-SOURCE-LEDGER-READ",
               "permits": "AUTHOR-PERMIT-READ", "launch_lock": "AUTHOR-LOCK-BUSY",
               "exceptions": "AUTHOR-EXCEPTION-SOURCE", "permit_write": "AUTHOR-PERMIT-WRITE",
               "refusal_audit": None}
PERMIT_FIELDS = ("type", "permit_id", "run_id", "work_id", "linked_ids_sha256", "authors_before", "lock_before",
                 "family", "role", "risk", "auth_resolved", "model", "policy", "exception_id", "entry", "issued_at")
VOID_FIELDS = ("type", "permit_id", "run_id", "work_id", "reason", "entry", "voided_at")
REFUSAL_FIELDS = ("ts", "entry", "run_id", "work_id", "engine", "role", "risk", "linked_ids", "authors_before",
                  "lock_before", "code", "codes", "exception_id")
APPROVAL_FIELDS = ("approval_id", "work_id", "linked_ids_sha256", "existing_families", "lock_family", "new_family",
                   "role", "purpose", "input_sha256", "not_before", "expires_at", "max_uses", "approval_source",
                   "approval_digest", "verified_by", "acknowledges", "lock_exception")
ACKNOWLEDGES = ("dual_author_blocks_model_review", "second_family_becomes_author")
POLICY_FIELDS = ("version", "families", "high_risk_pair", "writer_lock_scope", "model_families",
                 "harness_families", "default_harness", "ack_required", "reviewers", "probe")
PROBE_POLICY_FIELDS = ("task_markers", "instruction_files", "input_sha256")
REQUEST_DEFAULTS = {"risk": None, "model": None, "harness": None, "agent": None, "today": None, "ack": None,
                    "probe": False, "purpose": None, "task": "", "business_input": False, "cwd": None,
                    "home": None, "exception_id": None, "input_sha256": None, "now": None, "run_id": None,
                    "auth_resolved": None}
SNAPSHOT_DEFAULTS = {"registry": {}, "log_registry": {}, "runs": [], "permits": [], "adhoc": {},
                     "exceptions": None, "unavailable": []}
REGISTRY_FIELDS = ("high_risk", "parents", "authors", "writer_locks")
CWD_FIELDS = ("absolute", "real_directory", "owned_by_caller", "mode", "empty", "names_on_path")
HOME_FIELDS = ("instruction_files_present", "entry_masks_home")
DEFAULT_AT = "2026-01-02T00:00:00+00:00"


class _Refusal(Exception):
    def __init__(self, codes, reason=None):
        super().__init__(codes[0])
        self.codes, self.reason = list(codes), reason


def _deny(reason):
    raise _Refusal([DENIED], reason)


def _name(value):
    return isinstance(value, str) and NAME.fullmatch(value) is not None


def _strings(value, allow_empty=False):
    return (isinstance(value, list) and (allow_empty or bool(value))
            and all(isinstance(item, str) and item for item in value))


# ---- managed policy -------------------------------------------------------------------------------------------
def _policy(policy):
    """The managed policy, or AUTHOR-POLICY: every field present, nothing extra, every name known."""
    try:
        if not isinstance(policy, dict) or set(policy) != set(POLICY_FIELDS):
            raise ValueError()
        families, pair = policy["families"], policy["high_risk_pair"]
        probe = policy["probe"]
        valid = (
            policy["version"] == POLICY
            and _strings(families) and all(_name(f) for f in families) and HUMAN not in families
            and len(families) == len(set(families))
            and isinstance(pair, list) and len(pair) == 2 and len(set(pair)) == 2 and set(pair) <= set(families)
            and policy["writer_lock_scope"] in ("all", "high")
            and all(isinstance(policy[key], dict) and all(isinstance(k, str) and k for k in policy[key])
                    for key in ("model_families", "harness_families", "default_harness", "reviewers"))
            and set(policy["model_families"].values()) <= set(families)
            and set(policy["harness_families"].values()) <= set(families)
            and set(policy["default_harness"]) <= set(families)
            and all(isinstance(v, str) and v for v in policy["default_harness"].values())
            and set(policy["reviewers"]) <= set(families)
            and all(_strings(v) and set(v) <= set(families) and author not in v
                    for author, v in policy["reviewers"].items())
            and type(policy["ack_required"]) is bool
            and isinstance(probe, dict) and set(probe) == set(PROBE_POLICY_FIELDS)
            and _strings(probe["task_markers"]) and _strings(probe["instruction_files"])
            and not any("/" in name for name in probe["instruction_files"])
            and isinstance(probe["input_sha256"], str) and DIGEST.fullmatch(probe["input_sha256"]) is not None)
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise _Refusal(["AUTHOR-POLICY"])
    return policy


# ---- request and snapshot shapes ------------------------------------------------------------------------------
def _request(request, policy):
    required = {"engine", "role", "work_id"}
    if (not isinstance(request, dict) or not required <= set(request)
            or set(request) - required - set(REQUEST_DEFAULTS)):
        _deny("request")
    merged = {**REQUEST_DEFAULTS, **request}
    optional = ("risk", "model", "harness", "agent", "today", "ack", "purpose", "exception_id", "input_sha256",
                "now", "run_id", "auth_resolved")
    if (merged["engine"] not in policy["families"] or merged["role"] not in ROLES or not _name(merged["work_id"])
            or any(merged[key] is not None and not isinstance(merged[key], str) for key in optional)
            or type(merged["probe"]) is not bool or type(merged["business_input"]) is not bool
            or not isinstance(merged["task"], str)
            or any(merged[key] is not None and not isinstance(merged[key], dict) for key in ("cwd", "home"))):
        _deny("request")
    return merged


def _snapshot(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) - set(SNAPSHOT_DEFAULTS):
        _deny("snapshot")
    merged = {**SNAPSHOT_DEFAULTS, **snapshot}
    unavailable, adhoc = merged["unavailable"], merged["adhoc"]
    if (not isinstance(unavailable, list)
            or any(not isinstance(item, str) or item not in UNAVAILABLE for item in unavailable)
            or not isinstance(adhoc, dict)
            or any(not _name(key) or not isinstance(value, dict) or set(value) != {"day", "agent"}
                   or not all(isinstance(v, str) and v for v in value.values()) for key, value in adhoc.items())):
        _deny("snapshot")
    return merged


# ---- probe purpose and isolation ------------------------------------------------------------------------------
def _label(text):
    return " ".join(re.sub(r"[\s_\-./:]+", " ", unicodedata.normalize("NFKC", text or "").casefold()).split())


def task_marked(task, markers):
    """Whether a task label reads as a probe or status check. Labels only; the prompt is never scanned."""
    label = _label(task)
    for marker in map(_label, markers):
        if not marker:
            continue
        if marker.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])", label):
                return True
        elif marker in label:
            return True
    return False


def _purpose(request, markers):
    probe, purpose = request["probe"], request["purpose"]
    if purpose and purpose not in PURPOSES:
        raise _Refusal(["PROBE-PURPOSE-CONFLICT"], "purpose-value")
    if probe and purpose == "work":
        raise _Refusal(["PROBE-PURPOSE-CONFLICT"], "probe-with-work")
    if not probe and purpose in ("probe", "status"):
        raise _Refusal(["PROBE-PURPOSE-CONFLICT"], "purpose-without-probe")
    if not probe and not purpose and task_marked(request["task"], markers):
        raise _Refusal(["PROBE-PURPOSE-AMBIGUOUS"])
    return purpose or ("probe" if probe else "work")


def _probe_cwd(cwd, names):
    """None: the runner makes its own empty 0700 directory. Otherwise the caller's observation must prove it."""
    if cwd is None:
        return
    refuse = lambda why: _Refusal(["PROBE-ISOLATION-CWD"], why)
    if (set(cwd) != set(CWD_FIELDS) or not isinstance(cwd["mode"], str)
            or not all(type(cwd[key]) is bool for key in ("absolute", "real_directory", "owned_by_caller", "empty"))
            or not _strings(cwd["names_on_path"], allow_empty=True)):
        raise refuse("observation")
    if not cwd["absolute"]:
        raise refuse("absolute")
    if not cwd["real_directory"]:
        raise refuse("real-directory")
    if not cwd["owned_by_caller"] or cwd["mode"] != "0700":
        raise refuse("owner-mode")
    if not cwd["empty"]:
        raise refuse("empty")
    if set(cwd["names_on_path"]) & set(names):
        raise refuse("instruction-file")


def _probe_home(home):
    """User-level instruction files must be absent or provably masked on this entry; no observation, no proof."""
    if (not isinstance(home, dict) or set(home) != set(HOME_FIELDS)
            or not all(type(home[key]) is bool for key in HOME_FIELDS)):
        raise _Refusal(["PROBE-ISOLATION-UNVERIFIED"], "observation")
    if home["instruction_files_present"] and not home["entry_masks_home"]:
        raise _Refusal(["PROBE-ISOLATION-UNVERIFIED"])


# ---- sources of one snapshot ----------------------------------------------------------------------------------
def linked_ids(work_id, relations):
    """WORK_ID and every task the relations link to it, in either direction and over any number of hops."""
    ids = {work_id}
    while True:
        before = set(ids)
        for endpoints, _ in relations:
            if ids & endpoints:
                ids |= endpoints
        if ids == before:
            return ids


def _registries(snapshot):
    """[(document, trusted)]: the log directory's registry is never trusted; the managed one is."""
    registries = [(snapshot["log_registry"], False), (snapshot["registry"], True)]
    for document, _ in registries:
        if (not isinstance(document, dict) or set(document) - set(REGISTRY_FIELDS)
                or not isinstance(document.get("high_risk", []), list)
                or not all(isinstance(item, str) for item in document.get("high_risk", []))
                or not all(isinstance(document.get(key, {}), dict) for key in ("parents", "authors", "writer_locks"))):
            raise _Refusal(["AUTHOR-SOURCE-REGISTRY-FORMAT"])
    return registries


def _relations(registries):
    relations = []
    for document, _ in registries:
        for child, related in document.get("parents", {}).items():
            related = [related] if isinstance(related, str) else related
            tokens = [child] + (list(related) if isinstance(related, (list, dict)) else [])
            relations.append(({t for t in tokens if _name(t)},
                              not isinstance(related, list) or not all(_name(t) for t in tokens)))
    return relations


def _valid_record(record, families):
    # FAMILIES is a tuple: membership never hashes an untrusted value.
    if not isinstance(record, dict):
        return False
    keys = set(record)
    if record.get("type") == "permit":
        return ({"type", "permit_id", "work_id", "family", "role", "exception_id"} <= keys <= set(PERMIT_FIELDS)
                and _name(record["permit_id"]) and _name(record["work_id"]) and record["family"] in families
                and record["role"] in ROLES and (record["exception_id"] is None or _name(record["exception_id"])))
    if record.get("type") == "void":
        return ({"type", "permit_id", "work_id", "reason"} <= keys <= set(VOID_FIELDS)
                and _name(record["permit_id"]) and _name(record["work_id"]) and record["reason"] == "never_started")
    return False


def active_permits(records):
    """Permits not voided; a void counts only for a permit recorded before it."""
    permits, voided = {}, set()
    for record in records:
        if record["type"] == "permit":
            permits.setdefault(record["permit_id"], record)
        elif record["permit_id"] in permits:
            voided.add(record["permit_id"])
    return [permit for key, permit in permits.items() if key not in voided]


def _view(request, snapshot, policy, warnings):
    """(ids, authors, registered writer locks, valid permit records, registries) of the linked task set."""
    families = tuple(policy["families"])
    unavailable = snapshot["unavailable"]
    if "launch_lock" in unavailable:
        raise _Refusal([UNAVAILABLE["launch_lock"]])
    if "registry" in unavailable:
        raise _Refusal([UNAVAILABLE["registry"]])
    registries = _registries(snapshot)
    relations = _relations(registries)
    ids = linked_ids(request["work_id"], relations)
    if any(invalid and ids & endpoints for endpoints, invalid in relations):
        raise _Refusal(["AUTHOR-SOURCE-RELATIONS"])
    if any(invalid for _, invalid in relations):
        warnings.append("relations-outside-set-skipped")
    for document, trusted in registries:
        for wid, values in document.get("authors", {}).items():
            if wid in ids and (not isinstance(values, list) or any(v not in (*families, HUMAN) for v in values)):
                raise _Refusal(["AUTHOR-SOURCE-REGISTRY-ENTRY"])
        # A writer lock is trusted only from the managed registry, as a human author is, and names one engine.
        for wid, family in document.get("writer_locks", {}).items():
            if wid in ids and (not trusted or family not in families):
                raise _Refusal(["AUTHOR-SOURCE-REGISTRY-ENTRY"])
    if "runs" in unavailable:
        raise _Refusal([UNAVAILABLE["runs"]])
    runs = snapshot["runs"]
    if not isinstance(runs, list) or not all(isinstance(row, dict) for row in runs):
        raise _Refusal(["AUTHOR-SOURCE-LEDGER-FORMAT"])
    for row in runs:
        if not _name(row.get("work_id")):
            warnings.append("run-row-without-task-skipped")
        elif row["work_id"] in ids and row.get("role") != "review":
            actual = row.get("actual_engine_families")
            actual = [] if actual is None else actual
            if (row.get("engine") not in families or not isinstance(actual, list)
                    or any(family not in families for family in actual)):
                raise _Refusal(["AUTHOR-SOURCE-LEDGER-ENTRY"])
    if "permits" in unavailable:
        raise _Refusal([UNAVAILABLE["permits"]])
    if not isinstance(snapshot["permits"], list):
        raise _Refusal(["AUTHOR-PERMIT-FORMAT"])
    records = []
    for record in snapshot["permits"]:
        if _valid_record(record, families):
            records.append(record)
        elif not isinstance(record, dict) or (_name(record.get("work_id")) and record["work_id"] in ids):
            raise _Refusal(["AUTHOR-PERMIT-FORMAT"])
        else:
            warnings.append("permit-record-outside-set-skipped")
    # Authors: registries, every non-review run (failed, empty and timed-out alike) and every permit not voided.
    if any(HUMAN in document.get("authors", {}).get(wid, []) for document, trusted in registries if not trusted
           for wid in ids):
        _deny("human-author-untrusted")
    authors = set()
    for document, _ in registries:
        for wid in ids:
            authors.update(document.get("authors", {}).get(wid, []))
    for row in runs:
        if _name(row.get("work_id")) and row["work_id"] in ids and row.get("role") != "review":
            authors.add(row["engine"])
            authors.update(row.get("actual_engine_families") or [])
    for permit in active_permits(records):
        if permit["work_id"] in ids and permit["role"] != "review":
            authors.add(permit["family"])
    registered = {wid: family for wid, family in registries[1][0].get("writer_locks", {}).items() if wid in ids}
    return ids, frozenset(authors), registered, records, registries


def _risk(request, ids, registries, policy):
    registered = ids & {wid for document, _ in registries for wid in document.get("high_risk", [])}
    risk = request["risk"]
    if request["role"] == "review" and not risk:
        _deny("risk-required")
    if registered:
        if risk == "normal":
            _deny("risk-conflict")
        risk = "high"
    risk = risk or "normal"
    if risk not in ("normal", "high"):
        _deny("risk-invalid")
    if request["role"] == "review" and risk == "high" and request["engine"] not in policy["high_risk_pair"]:
        _deny("review-high-risk-family")
    return risk


# ---- the pure decision ----------------------------------------------------------------------------------------
def model_family(model, policy):
    """The family of a resolved model name from the managed table; None when unresolved or not in the table."""
    return policy["model_families"].get(model) if model else None


def writer_lock(authors, registered):
    """(state, family): the registered lock, else the single engine family of the authors, none, or unknown."""
    values = set(registered.values())
    if len(values) > 1:
        return CONFLICT, None
    if values:
        return LOCKED, values.pop()
    engines = set(authors) - {HUMAN}
    if not engines:
        return NONE, None
    if len(engines) == 1:
        return LOCKED, next(iter(engines))
    return UNKNOWN, None


def launch_decision(engine, role, risk, model, harness, authors, registered, policy, exception=None):
    """{allowed, codes, lock}: every violated rule in order, the first one reported. A review takes no lock and
    adds no author; the review gate decides it. A declaration (ack) is never an input here."""
    authors = frozenset(authors)
    lock = writer_lock(authors, registered)
    if role == "review":
        return {"allowed": True, "codes": [], "lock": lock}
    resolved = model_family(model, policy)
    families = {engine} | ({resolved} if resolved else set())
    codes = []
    if len(families) > 1:
        codes.append("AUTHOR-FAMILY-PREFLIGHT")
    harness = harness or policy["default_harness"].get(engine)
    if risk == "high" and model and resolved is None and policy["harness_families"].get(harness) != engine:
        codes.append("AUTHOR-FAMILY-UNPROVABLE")
    state, family = lock if policy["writer_lock_scope"] == "all" or risk == "high" else (NONE, None)
    if state == CONFLICT:
        codes.append("AUTHOR-LOCK-CONFLICT")
    elif state == UNKNOWN:
        codes.append("AUTHOR-LOCK-UNKNOWN")
    elif state == LOCKED and engine != family and not (exception and exception["lock_exception"]):
        codes.append("AUTHOR-LOCK-MISMATCH")
    pair = set(policy["high_risk_pair"])
    if risk == "high" and pair <= (authors | families) and not exception:
        codes.append("AUTHOR-LAUNCH-DUAL-EXISTING" if pair <= authors else "AUTHOR-LAUNCH-SECOND-FAMILY")
    return {"allowed": not codes, "codes": codes, "lock": lock}


# ---- managed exceptions ---------------------------------------------------------------------------------------
def linked_ids_sha256(ids):
    """The digest a permit and an exception approval name the linked task set by."""
    return hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()


def _instant(value):
    try:
        at = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return at if at is not None and at.tzinfo is not None else None


def _single_line(value):
    return isinstance(value, str) and value.strip() != "" and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def valid_approval(item, families):
    """One approval names one launch exactly: no field missing or extra, no wildcard, no generic value."""
    if not isinstance(item, dict) or set(item) != set(APPROVAL_FIELDS):
        return False
    existing = item["existing_families"]
    begin, end = _instant(item["not_before"]), _instant(item["expires_at"])
    return bool(_name(item["approval_id"]) and _name(item["work_id"])
                and all(isinstance(item[k], str) and DIGEST.fullmatch(item[k])
                        for k in ("linked_ids_sha256", "input_sha256", "approval_digest"))
                and isinstance(existing, list) and all(f in (*families, HUMAN) for f in existing)
                and existing == sorted(set(existing))
                and (item["lock_family"] is None or item["lock_family"] in families)
                and item["new_family"] in families and item["role"] in ("code", "other") and item["purpose"] == "work"
                and begin is not None and end is not None and begin < end
                and type(item["max_uses"]) is int and item["max_uses"] == 1
                and _single_line(item["approval_source"]) and _single_line(item["verified_by"])
                and isinstance(item["acknowledges"], list) and all(isinstance(a, str) for a in item["acknowledges"])
                and sorted(item["acknowledges"]) == list(ACKNOWLEDGES)
                and type(item["lock_exception"]) is bool)


def valid_exceptions(document, families):
    """The whole managed document: {version 1, approvals, revoked}, every approval valid and every id unique."""
    if not isinstance(document, dict) or set(document) != {"version", "approvals", "revoked"}:
        return False
    approvals, revoked = document["approvals"], document["revoked"]
    if (type(document["version"]) is not int or document["version"] != 1 or not isinstance(approvals, list)
            or not isinstance(revoked, list)):
        return False
    ids = [item.get("approval_id") if isinstance(item, dict) else None for item in approvals]
    return (all(valid_approval(item, families) for item in approvals) and len(ids) == len(set(ids))
            and all(_name(i) for i in revoked) and len(revoked) == len(set(revoked)))


def _exception(request, snapshot, policy, ids, authors, lock, records, purpose, stage):
    """(verified, None) when the managed approval is exactly this launch, else (None, code). The early check
    has no prompt yet; the permit check always compares the approved input digest. Any permit naming the
    approval consumed it, voided or not."""
    approval_id = request["exception_id"]
    if not _name(approval_id):
        return None, "AUTHOR-EXCEPTION-FORMAT"
    document = snapshot["exceptions"]
    if "exceptions" in snapshot["unavailable"] or document is None:
        return None, "AUTHOR-EXCEPTION-SOURCE"
    if not valid_exceptions(document, policy["families"]):
        return None, "AUTHOR-EXCEPTION-FORMAT"
    approval = next((item for item in document["approvals"] if item["approval_id"] == approval_id), None)
    if approval is None:
        return None, "AUTHOR-EXCEPTION-NOT-FOUND"
    if approval_id in document["revoked"]:
        return None, "AUTHOR-EXCEPTION-REVOKED"
    at = _instant(request["now"])
    if at is None or not _instant(approval["not_before"]) <= at < _instant(approval["expires_at"]):
        return None, "AUTHOR-EXCEPTION-EXPIRED"
    if sum(r["type"] == "permit" and r["exception_id"] == approval_id for r in records) >= approval["max_uses"]:
        return None, "AUTHOR-EXCEPTION-CONSUMED"
    if approval["input_sha256"] == policy["probe"]["input_sha256"] or purpose != "work":
        return None, "AUTHOR-EXCEPTION-PROBE"
    if (approval["work_id"] != request["work_id"] or approval["new_family"] != request["engine"]
            or approval["role"] != request["role"] or approval["purpose"] != purpose):
        return None, "AUTHOR-EXCEPTION-MISMATCH"
    state, family = lock
    if (state not in (NONE, LOCKED) or approval["lock_family"] != (family if state == LOCKED else None)
            or approval["linked_ids_sha256"] != linked_ids_sha256(ids)
            or approval["existing_families"] != sorted(authors)):
        return None, "AUTHOR-EXCEPTION-SCOPE"
    if stage == "permit" and request["input_sha256"] != approval["input_sha256"]:
        return None, "AUTHOR-EXCEPTION-INPUT"
    return {"approval_id": approval_id, "lock_exception": approval["lock_exception"]}, None


# ---- one launch -----------------------------------------------------------------------------------------------
def evaluate(request, snapshot, policy, stage="early"):
    """The decision of one launch over one snapshot; the inputs are never modified.

    STAGE is early (no permit, an exception's input digest not compared yet) or permit (the authoritative check
    a permit is written after). Every refusal is fail-closed and carries its fixed code(s)."""
    if stage not in ("early", "permit"):
        raise ValueError("stage")
    facts = {"work_id": None, "engine": None, "role": None, "risk": None, "linked_ids": None,
             "authors_before": None, "lock_before": None}
    warnings, state = [], {"purpose": None, "exception_id": None}

    def result(allowed, codes, reason=None, affects_lock=False):
        return {"allowed": allowed, "code": codes[0] if codes else None, "codes": list(codes), "reason": reason,
                "missing_approval": MISSING_APPROVAL.get(codes[0]) if codes else None,
                "purpose": state["purpose"], "affects_lock": affects_lock, "exception_id": state["exception_id"],
                **facts, "warnings": warnings}

    try:
        policy = _policy(copy.deepcopy(policy))
        request = _request(copy.deepcopy(request), policy)
        facts.update(work_id=request["work_id"], engine=request["engine"], role=request["role"])
        snapshot = _snapshot(copy.deepcopy(snapshot))
        if request["probe"] and request["business_input"]:
            raise _Refusal(["PROBE-INPUT"])
        state["purpose"] = purpose = _purpose(request, policy["probe"]["task_markers"])
        if request["exception_id"] and request["probe"]:
            raise _Refusal(["AUTHOR-EXCEPTION-PROBE"])
        if request["exception_id"] and request["role"] == "review":
            raise _Refusal(["AUTHOR-EXCEPTION-MISMATCH"])
        if request["role"] == "review":
            if request["probe"]:
                raise _Refusal(["PROBE-IDENTITY"])
            if request["ack"]:
                _deny("review-ack")
        ids, authors, registered, records, registries = _view(request, snapshot, policy, warnings)
        lock = writer_lock(authors, registered)
        facts.update(linked_ids=sorted(ids), authors_before=sorted(authors),
                     lock_before={"state": lock[0], "family": lock[1]})
        facts["risk"] = risk = _risk(request, ids, registries, policy)
        if request["role"] == "review":
            # The review gate under the same snapshot: no author may review, nor a family the authors do not allow.
            if request["engine"] in authors:
                _deny("review-author")
            if not authors:
                _deny("review-no-author")
            if any(request["engine"] not in policy["reviewers"].get(a, []) for a in authors - {HUMAN}):
                _deny("review-pairing")
            return result(True, [])
        info = snapshot["adhoc"].get(request["work_id"])
        independent = (info is not None and info["day"] == request["today"]
                       and info["agent"] == request["agent"])
        linked_formal = any(wid not in snapshot["adhoc"] for wid in ids)
        if info is not None and not independent and not linked_formal:
            _deny("adhoc-identity")
        formal = not independent or linked_formal
        if independent and not formal and request["role"] != "other":
            _deny("adhoc-role")
        if request["probe"]:
            if request["role"] != "other" or formal:
                raise _Refusal(["PROBE-IDENTITY"])
            _probe_cwd(request["cwd"], policy["probe"]["instruction_files"])
            _probe_home(request["home"])
        decide = lambda exception=None: launch_decision(
            request["engine"], request["role"], risk, request["model"], request["harness"], authors, registered,
            policy, exception)
        exception = None
        if request["exception_id"]:
            exception, code = _exception(request, snapshot, policy, ids, authors, lock, records, purpose, stage)
            if code:
                raise _Refusal([code] + [c for c in decide()["codes"] if c != code])
        decision = decide(exception)
        if not decision["allowed"]:
            raise _Refusal(decision["codes"])
        expected = "%s:%s:%s" % (request["work_id"], request["engine"], request["role"])
        if policy["ack_required"] and (formal or request["ack"]) and request["ack"] != expected:
            _deny("ack")
        if stage == "permit" and "permit_write" in snapshot["unavailable"]:
            raise _Refusal([UNAVAILABLE["permit_write"]])
        state["exception_id"] = exception["approval_id"] if exception else None
        return result(True, [], affects_lock=True)
    except _Refusal as refusal:
        state["exception_id"] = None
        return result(False, refusal.codes, refusal.reason)
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError):
        # Anything the shape checks did not foresee still refuses: fail closed, never launch.
        state["exception_id"] = None
        return result(False, [DENIED], "unexpected")


# ---- records --------------------------------------------------------------------------------------------------
def permit_sha256(record):
    """The digest of a permit's canonical JSON (keys sorted, no spaces, UTF-8 kept)."""
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def refusal_record(request, result, at, entry):
    """The refusal audit record: identities, families and codes only, never a prompt or source content."""
    request = request if isinstance(request, dict) else {}
    run_id, exception_id = request.get("run_id"), request.get("exception_id")
    return {"ts": at, "entry": entry, "run_id": run_id if isinstance(run_id, str) else None,
            "work_id": result["work_id"], "engine": result["engine"], "role": result["role"],
            "risk": result["risk"], "linked_ids": result["linked_ids"], "authors_before": result["authors_before"],
            "lock_before": result["lock_before"], "code": result["code"], "codes": result["codes"],
            "exception_id": exception_id if isinstance(exception_id, str) else None}


def issue_permit(request, snapshot, policy, permit_id, at=DEFAULT_AT, entry="host"):
    """The authoritative check as one pure step: {result, permit, permit_sha256, refusal, audit_written}.

    A real wrapper does this under its global launch lock, appends the permit as one line and syncs it to disk
    before any model process starts. Here nothing is written; the caller decides what to keep."""
    result = evaluate(request, snapshot, policy, stage="permit")
    if not result["allowed"]:
        unavailable = snapshot.get("unavailable", []) if isinstance(snapshot, dict) else []
        written = not (isinstance(unavailable, list) and "refusal_audit" in unavailable)
        return {"result": result, "permit": None, "permit_sha256": None,
                "refusal": refusal_record(request, result, at, entry), "audit_written": written}
    record = {"type": "permit", "permit_id": permit_id, "run_id": request.get("run_id"),
              "work_id": result["work_id"], "linked_ids_sha256": linked_ids_sha256(result["linked_ids"]),
              "authors_before": result["authors_before"], "lock_before": result["lock_before"],
              "family": result["engine"], "role": result["role"], "risk": result["risk"],
              "auth_resolved": request.get("auth_resolved"), "model": request.get("model") or None,
              "policy": POLICY, "exception_id": result["exception_id"], "entry": entry, "issued_at": at}
    return {"result": result, "permit": record, "permit_sha256": permit_sha256(record), "refusal": None,
            "audit_written": None}


def permit_evidence(row, rows, records, require=False, outer_run_id=None):
    """(status, errors) of one run record's launch permit, for a delivery check.

    A v2 row names exactly one permit, by id and digest, of its own task, family and role and its own run (or
    the outer run an isolated entry signed for, OUTER_RUN_ID). No other row may name it, a voided permit cannot
    stand behind a recorded run, and the actual families are the permit's one family. Older rows are legacy:
    reported, and failed only with REQUIRE (delivery entries)."""
    if not isinstance(row, dict) or not isinstance(rows, list):
        return "invalid", ["run record unreadable"]
    if row.get("author_launch_policy") != POLICY:
        return "legacy", (["launch permit required: legacy row"] if require else [])
    permit_id = row.get("launch_permit_id")
    if not _name(permit_id):
        return "missing", ["launch permit missing"]
    if not isinstance(records, list) or not all(isinstance(r, dict) and "type" in r for r in records):
        return "invalid", ["launch permit ledger unreadable"]
    issued = [r for r in records if r["type"] == "permit" and r.get("permit_id") == permit_id]
    if len(issued) != 1:
        return ("missing", ["launch permit missing"]) if not issued else ("invalid", ["launch permit not unique"])
    permit, = issued
    errors = []
    if sum(isinstance(r, dict) and r.get("launch_permit_id") == permit_id for r in rows) != 1:
        errors.append("launch permit named by several runs")
    if permit_sha256(permit) != row.get("launch_permit_sha256"):
        errors.append("launch permit digest mismatch")
    if (permit.get("work_id") != row.get("work_id") or permit.get("family") != row.get("engine")
            or permit.get("role") != row.get("role")):
        errors.append("launch permit identity mismatch")
    if permit.get("run_id") != row.get("run_id") and not (outer_run_id and permit.get("run_id") == outer_run_id):
        errors.append("launch permit run mismatch")
    if any(r["type"] == "void" and r.get("permit_id") == permit_id for r in records):
        errors.append("launch permit voided but the run is recorded")
    if row.get("actual_engine_families") != [permit.get("family")]:
        errors.append("actual engine families differ from the launch permit")
    return ("invalid" if errors else "verified"), errors


# ---- fictitious vectors ---------------------------------------------------------------------------------------
def _compare(expect, result, out=None):
    """Names of the expected keys the outcome does not match (only names, never values)."""
    mismatches = []
    for key, value in expect.items():
        if key == "permit":
            permit = (out or {}).get("permit")
            if value is None:
                if permit is not None:
                    mismatches.append("permit")
            elif not isinstance(permit, dict) or any(permit.get(k) != v for k, v in value.items()):
                mismatches.append("permit")
        elif key not in result or result[key] != value:
            mismatches.append(key)
    return mismatches


def _launch(step, snapshot, policy, label):
    stage = step.get("stage", "early")
    if stage == "permit":
        out = issue_permit(step["request"], snapshot, policy, step.get("permit_id", "p-" + label),
                           step.get("at", DEFAULT_AT), step.get("entry", "host"))
        return out["result"], out
    return evaluate(step["request"], snapshot, policy, stage), None


def run_document(document):
    """[(label, ok, mismatched keys)] for every vector and every sequence step of a vectors document."""
    if (not isinstance(document, dict) or document.get("format") != VECTORS_FORMAT
            or not isinstance(document.get("policy"), dict) or not isinstance(document.get("vectors"), list)
            or not isinstance(document.get("sequences"), list)):
        raise ValueError("vectors document")
    outcomes, labels = [], set()
    for vector in document["vectors"] + document["sequences"]:
        if not isinstance(vector, dict) or not _name(vector.get("id")) or vector["id"] in labels:
            raise ValueError("vector id")
        labels.add(vector["id"])
    for vector in document["vectors"]:
        policy = {**document["policy"], **vector.get("policy", {})}
        result, out = _launch(vector, vector.get("snapshot", {}), policy, vector["id"])
        mismatches = _compare(vector["expect"], result, out)
        outcomes.append((vector["id"], not mismatches, mismatches))
    for sequence in document["sequences"]:
        policy = {**document["policy"], **sequence.get("policy", {})}
        snapshot = copy.deepcopy({**SNAPSHOT_DEFAULTS, **sequence.get("snapshot", {})})
        for number, step in enumerate(sequence["steps"], 1):
            label = "%s#%d" % (sequence["id"], number)
            if "run" in step:
                snapshot["runs"].append(copy.deepcopy(step["run"]))
                continue
            if "void" in step:
                snapshot["permits"].append({"type": "void", "reason": "never_started", **step["void"]})
                continue
            result, out = _launch(step, snapshot, policy, label)
            if out and out["permit"]:
                snapshot["permits"].append(out["permit"])
            mismatches = _compare(step["expect"], result, out)
            outcomes.append((label, not mismatches, mismatches))
    return outcomes


def _strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("constant")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def main(argv):
    if len(argv) != 2:
        print("用法：python3 author_gate.py 〈向量文件.json〉", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as stream:
            outcomes = run_document(_strict_json(stream.read()))
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError):
        print("向量文件无法读取或格式无效；内容不输出。", file=sys.stderr)
        return 2
    for label, ok, mismatches in outcomes:
        print(("通过 " if ok else "不符 ") + label + ("" if ok else "：" + "、".join(mismatches)))
    failed = sum(not ok for _, ok, _ in outcomes)
    print("共 %d 条，通过 %d 条，不符 %d 条。参考判定不是强制门禁。" % (len(outcomes), len(outcomes) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
