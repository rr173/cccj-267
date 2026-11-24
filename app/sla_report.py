#!/usr/bin/env python3
"""Offline, restatable SLA processing-time reports.

The program uses only the Python standard library.  Time is represented as
integer UTC minutes since 1970-01-01T00:00Z.  Intervals are half-open:
[start, end).  No per-minute work rows are created; all calculations sweep
interval and level-change boundaries.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "1.0.0"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

TIMELINESS_ON_TIME = "on_time"
TIMELINESS_TIMEOUT = "timeout"
TIMELINESS_INDETERMINATE = "indeterminate"


class InputError(Exception):
    """Invalid command input."""


# ---------------------------------------------------------------------------
# Time and interval helpers
# ---------------------------------------------------------------------------


def parse_utc_minute(value: Any, context: str) -> int:
    if not isinstance(value, str) or not value:
        raise InputError(f"{context}: expected an ISO-8601 UTC minute string, got {value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InputError(f"{context}: invalid timestamp {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        raise InputError(f"{context}: timestamp {value!r} is not timezone-aware; use UTC/Z")
    dt = dt.astimezone(timezone.utc)
    if dt.second != 0 or dt.microsecond != 0:
        raise InputError(
            f"{context}: timestamp {value!r} is not a whole UTC minute boundary "
            "(seconds and fractions must be zero)"
        )
    return int((dt - EPOCH) // timedelta(minutes=1))


def format_minute(minute: Optional[int]) -> Optional[str]:
    if minute is None:
        return None
    dt = EPOCH + timedelta(minutes=int(minute))
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def parse_interval(obj: Any, context: str) -> tuple[int, int]:
    if not isinstance(obj, dict):
        raise InputError(f"{context}: interval must be an object")
    start = parse_utc_minute(obj.get("start"), f"{context}.start")
    end = parse_utc_minute(obj.get("end"), f"{context}.end")
    if end < start:
        raise InputError(f"{context}: end {format_minute(end)} precedes start {format_minute(start)}")
    return start, end


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or adjacent half-open intervals.

    Adjacent [a,b), [b,c) merge because there is no minute between them.  A
    one-minute gap such as [a,b), [b+1,c) is preserved.
    """
    if not intervals:
        return []
    ordered = sorted((s, e) for s, e in intervals if e > s)
    merged: list[list[int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [(s, e) for s, e in merged]


def subtract_interval(start: int, end: int, removals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return [start,end) minus a merged/arbitrary interval union."""
    if end <= start:
        return []
    result: list[tuple[int, int]] = []
    cursor = start
    for raw_s, raw_e in sorted(removals):
        s = max(raw_s, start)
        e = min(raw_e, end)
        if e <= cursor:
            continue
        if s > cursor:
            result.append((cursor, min(s, end)))
        cursor = max(cursor, e)
        if cursor >= end:
            break
    if cursor < end:
        result.append((cursor, end))
    return [(s, e) for s, e in result if e > s]


def in_union(union: list[tuple[int, int]], minute: int) -> bool:
    if not union:
        return False
    starts = [item[0] for item in union]
    idx = bisect_right(starts, minute) - 1
    if idx < 0:
        return False
    start, end = union[idx]
    return start <= minute < end


def contains_with_starts(
    union: list[tuple[int, int]], starts: list[int], minute: int
) -> bool:
    if not union:
        return False
    idx = bisect_right(starts, minute) - 1
    if idx < 0:
        return False
    start, end = union[idx]
    return start <= minute < end


def intersect_intervals(
    left: list[tuple[int, int]], right: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Intersect two sorted/merged or unsorted interval unions."""
    left = merge_intervals(left)
    right = merge_intervals(right)
    i = j = 0
    result: list[tuple[int, int]] = []
    while i < len(left) and j < len(right):
        start = max(left[i][0], right[j][0])
        end = min(left[i][1], right[j][1])
        if start < end:
            result.append((start, end))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return result


def interval_json(interval: tuple[int, int]) -> dict[str, str]:
    return {"start": format_minute(interval[0]), "end": format_minute(interval[1]),
            "minutes": interval[1] - interval[0]}


# ---------------------------------------------------------------------------
# Evidence submissions and record versions
# ---------------------------------------------------------------------------


@dataclass
class Op:
    record_id: str
    version: int
    op: str
    submit_no: int
    op_index: int
    raw: dict[str, Any]
    kind: Optional[str] = None
    case_id: Optional[str] = None
    at: Optional[int] = None
    start: Optional[int] = None
    end: Optional[int] = None
    open_ended: bool = False
    service_point: Optional[str] = None
    level: Optional[str] = None
    to_level: Optional[str] = None
    contract_id: Optional[str] = None
    business_seq: int = 0
    selected: bool = False


def _require_str(raw: dict[str, Any], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise InputError(f"{context}.{key}: expected a non-empty string")
    return value


def normalize_op(raw: Any, submit_no: int, op_index: int) -> Op:
    context = f"submission {submit_no} record[{op_index}]"
    if not isinstance(raw, dict):
        raise InputError(f"{context}: record must be an object")
    record_id = _require_str(raw, "record_id", context)
    context = f"submission {submit_no} record {record_id}"
    try:
        version = int(raw.get("version"))
    except (TypeError, ValueError) as exc:
        raise InputError(f"{context}.version: expected integer") from exc
    if version < 1:
        raise InputError(f"{context}.version: must be >= 1")
    op_name = raw.get("op", "put")
    if op_name not in {"put", "revoke"}:
        raise InputError(f"{context}.op: must be put or revoke")

    kind = raw.get("kind")
    if op_name == "put" and kind not in {"intake", "completion", "waiting", "level_change"}:
        raise InputError(f"{context}.kind: unsupported kind {kind!r}")

    op = Op(
        record_id=record_id,
        version=version,
        op=op_name,
        submit_no=submit_no,
        op_index=op_index,
        raw=raw,
        kind=kind if isinstance(kind, str) else None,
        case_id=raw.get("case_id") if isinstance(raw.get("case_id"), str) else None,
        business_seq=int(raw.get("business_seq", raw.get("seq", 0)) or 0),
    )

    if op_name == "revoke":
        return op

    if kind == "intake":
        op.case_id = op.case_id or _require_str(raw, "case_id", context)
        op.at = parse_utc_minute(raw.get("at"), f"{context}.at")
        op.service_point = _require_str(raw, "service_point", context)
        op.level = _require_str(raw, "level", context)
        cid = raw.get("contract_id")
        op.contract_id = cid if isinstance(cid, str) and cid else None
    elif kind == "completion":
        op.case_id = op.case_id or _require_str(raw, "case_id", context)
        op.at = parse_utc_minute(raw.get("at"), f"{context}.at")
    elif kind == "waiting":
        op.case_id = op.case_id or _require_str(raw, "case_id", context)
        op.start = parse_utc_minute(raw.get("start"), f"{context}.start")
        end_value = raw.get("end")
        if end_value is None:
            op.end = None
            op.open_ended = True
        else:
            op.end = parse_utc_minute(end_value, f"{context}.end")
            if op.end <= op.start:  # type: ignore[operator]
                raise InputError(f"{context}: waiting end must be strictly after start")
    elif kind == "level_change":
        op.case_id = op.case_id or _require_str(raw, "case_id", context)
        op.at = parse_utc_minute(raw.get("at"), f"{context}.at")
        op.to_level = _require_str(raw, "to_level", context)
    else:  # pragma: no cover - guarded above
        raise InputError(context)
    return op


@dataclass
class Evidence:
    cutoff: int
    ops: list[Op]
    groups: dict[str, list[Op]]
    winners: dict[str, Op]

    def sources_for_case(self, case_id: str) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for rid, ops in self.groups.items():
            case_match = any(op.case_id == case_id for op in ops)
            if not case_match:
                continue
            for op in ops:
                refs.append(op_ref(op, selected=(self.winners.get(rid) is op)))
        refs.sort(key=lambda r: (r.get("submit_no", 0), r.get("op_index", 0), r["record_id"]))
        return refs


def op_ref(op: Op, selected: bool) -> dict[str, Any]:
    effective_state: str
    if not selected:
        effective_state = "superseded_or_lower_version"
    elif op.op == "revoke":
        effective_state = "revoked"
    else:
        effective_state = "active"
    return {
        "source_type": "business_record",
        "record_id": op.record_id,
        "version": op.version,
        "op": op.op,
        "kind": op.kind,
        "case_id": op.case_id,
        "submit_no": op.submit_no,
        "op_index": op.op_index,
        "business_seq": op.business_seq if op.kind == "level_change" else None,
        "selected_as_highest_visible_version": selected,
        "effective_state_as_of_cutoff": effective_state,
        "record": op.raw,
    }


def evidence_from_data(data: dict[str, Any], cutoff: int) -> Evidence:
    submissions = data.get("submissions", data if isinstance(data, list) else [])
    if not isinstance(submissions, list):
        raise InputError("evidence.submissions must be a list")

    ops: list[Op] = []
    seen_submissions: set[int] = set()
    for sub in submissions:
        if not isinstance(sub, dict):
            raise InputError("each submission must be an object")
        try:
            submit_no = int(sub.get("submit_no"))
        except (TypeError, ValueError) as exc:
            raise InputError("submission missing integer submit_no") from exc
        if submit_no in seen_submissions:
            raise InputError(f"duplicate submit_no {submit_no}")
        seen_submissions.add(submit_no)
        if submit_no > cutoff:
            continue
        records = sub.get("records", [])
        if not isinstance(records, list):
            raise InputError(f"submission {submit_no}.records must be a list")
        for index, raw in enumerate(records):
            ops.append(normalize_op(raw, submit_no, index))

    groups: dict[str, list[Op]] = {}
    for op in ops:
        groups.setdefault(op.record_id, []).append(op)

    winners: dict[str, Op] = {}
    for rid, group in groups.items():
        # Versions are primary.  Submission/order are only deterministic
        # tie-breakers for malformed duplicate versions.
        group.sort(key=lambda o: (o.version, o.submit_no, o.op_index))
        winner = group[-1]
        winners[rid] = winner
        winner.selected = True
        # A revoke may omit identity fields; recover them from its group.
        if winner.op == "revoke":
            for other in group:
                if other.case_id and not winner.case_id:
                    winner.case_id = other.case_id
                if other.kind and not winner.kind:
                    winner.kind = other.kind
        for op in group:
            if op.op == "revoke":
                for other in group:
                    if other.case_id and not op.case_id:
                        op.case_id = other.case_id
                    if other.kind and not op.kind:
                        op.kind = other.kind
    return Evidence(cutoff=cutoff, ops=ops, groups=groups, winners=winners)


def load_evidence(path: str | Path, cutoff: int) -> Evidence:
    p = Path(path)
    if p.is_dir():
        submissions: list[Any] = []
        files = sorted(p.glob("*.json"))
        if not files:
            raise InputError(f"evidence directory {p} contains no .json submission files")
        for file_path in files:
            item = load_json_object(file_path)
            if isinstance(item.get("submissions"), list):
                submissions.extend(item["submissions"])
            elif "submit_no" in item:
                submissions.append(item)
            else:
                raise InputError(
                    f"{file_path}: expected a submission object or an object with submissions[]"
                )
        return evidence_from_data({"submissions": submissions}, cutoff)

    if p.suffix.lower() == ".jsonl":
        submissions = []
        try:
            with p.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, 1):
                    line = line.strip()
                    if line:
                        try:
                            submissions.append(json.loads(line))
                        except json.JSONDecodeError as exc:
                            raise InputError(f"{p}:{line_no}: invalid JSONL: {exc}") from exc
        except FileNotFoundError as exc:
            raise InputError(f"file not found: {p}") from exc
        return evidence_from_data({"submissions": submissions}, cutoff)

    return evidence_from_data(load_json_object(p), cutoff)


# ---------------------------------------------------------------------------
# JSON / digest helpers
# ---------------------------------------------------------------------------


def load_json_object(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise InputError(f"file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid JSON in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError(f"{p}: top-level JSON value must be an object")
    return data


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def sha256_path(path: str | Path) -> str:
    p = Path(path)
    if p.is_file():
        return sha256_file(p)
    if not p.is_dir():
        raise InputError(f"input path not found: {p}")
    h = hashlib.sha256()
    files = sorted(x for x in p.rglob("*") if x.is_file())
    for file_path in files:
        rel = file_path.relative_to(p).as_posix().encode("utf-8")
        h.update(b"file:")
        h.update(rel)
        h.update(b"\0")
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                h.update(chunk)
        h.update(b"\0")
    return "sha256dir:" + h.hexdigest()


def canonical_digest(obj: Any) -> str:
    encoded = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Contract catalogs and calendars
# ---------------------------------------------------------------------------


@dataclass
class ContractCatalog:
    catalog_id: str
    version: int
    raw: dict[str, Any]
    visible_from_submit_no: Optional[int] = None


def select_contract_catalog(
    pack: dict[str, Any], catalog_id: str, cutoff: int, pin_version: Optional[int]
) -> ContractCatalog:
    items = pack.get("catalogs", pack.get("contract_catalogs", []))
    if not isinstance(items, list):
        raise InputError("contract catalog pack must contain catalogs list")
    matches = [c for c in items if isinstance(c, dict) and c.get("catalog_id") == catalog_id]
    if not matches:
        raise InputError(f"contract catalog {catalog_id!r} not found")
    if pin_version is not None:
        for item in matches:
            if int(item.get("version", -1)) == pin_version:
                return ContractCatalog(catalog_id, pin_version, item, item.get("visible_from_submit_no"))
        raise InputError(f"contract catalog {catalog_id!r} version {pin_version} not found")
    visible = []
    for item in matches:
        version = int(item.get("version", 0))
        available = item.get("visible_from_submit_no")
        if available is None or int(available) <= cutoff:
            visible.append((version, item))
    if not visible:
        raise InputError(f"no visible version of contract catalog {catalog_id!r} by cutoff {cutoff}")
    version, item = max(visible, key=lambda x: x[0])
    return ContractCatalog(catalog_id, version, item, item.get("visible_from_submit_no"))


def quota_from_spec(spec: Any, context: str) -> Optional[int]:
    if spec is None:
        return None
    if isinstance(spec, dict):
        value = spec.get("limit_minutes", spec.get("quota_minutes"))
    else:
        value = spec
    if value is None:
        return None
    try:
        quota = int(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{context}: quota must be integer minutes or null") from exc
    if quota < 0:
        raise InputError(f"{context}: quota must be >= 0")
    return quota


def resolve_contract(catalog: ContractCatalog, intake: Op) -> tuple[Optional[dict[str, Any]], list[str]]:
    problems: list[str] = []
    contracts = catalog.raw.get("contracts", [])
    if not isinstance(contracts, list):
        return None, ["contract_catalog_invalid_contracts"]
    assert intake.at is not None
    candidates: list[dict[str, Any]] = []
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        if intake.contract_id and contract.get("contract_id") != intake.contract_id:
            continue
        sps = contract.get("service_points", [])
        if sps != "*" and intake.service_point not in sps:
            continue
        effective_from = parse_utc_minute(contract.get("effective_from"), f"contract {contract.get('contract_id')}.effective_from")
        until = contract.get("effective_to")
        effective_to = parse_utc_minute(until, f"contract {contract.get('contract_id')}.effective_to") if until else None
        if effective_from <= intake.at and (effective_to is None or intake.at < effective_to):
            candidates.append(contract)
    if not candidates:
        problems.append("no_effective_contract_at_intake")
        return None, problems
    if len(candidates) > 1:
        ids = [str(c.get("contract_id")) for c in candidates]
        problems.append("ambiguous_effective_contract:" + ",".join(ids))
        return None, problems
    return candidates[0], problems


@dataclass
class Calendar:
    service_point: str
    version: int
    raw: dict[str, Any]
    calendar_id: Optional[str]
    coverage: list[tuple[int, int]]
    open_intervals: list[dict[str, Any]]
    open_union: list[tuple[int, int]]


def load_calendars(pack: dict[str, Any]) -> dict[str, list[Calendar]]:
    items = pack.get("calendars", pack.get("calendar_versions", []))
    if not isinstance(items, list):
        raise InputError("calendar pack must contain calendars list")
    result: dict[str, list[Calendar]] = {}
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise InputError(f"calendars[{idx}] must be an object")
        sp = _require_str(item, "service_point", f"calendars[{idx}]")
        version = int(item.get("version", 0))
        coverage_raw = item.get("coverage", [])
        if not isinstance(coverage_raw, list):
            raise InputError(f"calendar {sp} v{version}.coverage must be a list")
        coverage = merge_intervals([parse_interval(x, f"calendar {sp} v{version}.coverage") for x in coverage_raw])
        opens_raw = item.get("open_intervals", item.get("billable_intervals", []))
        if not isinstance(opens_raw, list):
            raise InputError(f"calendar {sp} v{version}.open_intervals must be a list")
        opens: list[dict[str, Any]] = []
        open_ivs: list[tuple[int, int]] = []
        for j, raw_iv in enumerate(opens_raw):
            iv = parse_interval(raw_iv, f"calendar {sp} v{version}.open_intervals[{j}]")
            oid = raw_iv.get("id") if isinstance(raw_iv, dict) else None
            opens.append({"id": oid, "start": iv[0], "end": iv[1]})
            open_ivs.append(iv)
        cal = Calendar(
            service_point=sp,
            version=version,
            raw=item,
            calendar_id=item.get("calendar_id") if isinstance(item.get("calendar_id"), str) else None,
            coverage=coverage,
            open_intervals=opens,
            open_union=merge_intervals(open_ivs),
        )
        result.setdefault(sp, []).append(cal)
    return result


def select_calendar(
    calendars: dict[str, list[Calendar]], service_point: str, cutoff: int, pins: dict[str, int]
) -> Optional[Calendar]:
    versions = calendars.get(service_point, [])
    if not versions:
        return None
    if service_point in pins:
        wanted = pins[service_point]
        for cal in versions:
            if cal.version == wanted:
                return cal
        raise InputError(f"calendar for {service_point!r} version {wanted} not found")
    visible = []
    for cal in versions:
        available = cal.raw.get("visible_from_submit_no")
        if available is None or int(available) <= cutoff:
            visible.append(cal)
    if not visible:
        return None
    return max(visible, key=lambda c: c.version)


def calendar_ref(cal: Optional[Calendar]) -> Optional[dict[str, Any]]:
    if cal is None:
        return None
    return {
        "source_type": "calendar",
        "service_point": cal.service_point,
        "calendar_id": cal.calendar_id,
        "version": cal.version,
        "visible_from_submit_no": cal.raw.get("visible_from_submit_no"),
        "coverage_intervals": [interval_json(x) for x in cal.coverage],
    }


# ---------------------------------------------------------------------------
# Report engine
# ---------------------------------------------------------------------------


@dataclass
class ReportConfig:
    as_of: int
    as_of_text: str
    cutoff: int
    catalog_id: str
    contract_version: Optional[int]
    calendar_pins: dict[str, int] = field(default_factory=dict)


def _quota_for_level(contract: Optional[dict[str, Any]], level: str) -> tuple[Optional[int], Optional[str]]:
    if contract is None:
        return None, "contract_unavailable"
    levels = contract.get("levels", {})
    if not isinstance(levels, dict) or level not in levels:
        return None, f"unknown_level:{level}"
    return quota_from_spec(levels[level], f"level {level}"), None


def _find_open_interval_id(cal: Optional[Calendar], minute_start: int) -> Optional[str]:
    if cal is None:
        return None
    for iv in cal.open_intervals:
        if iv["start"] <= minute_start < iv["end"]:
            return iv["id"] if isinstance(iv["id"], str) else None
    return None


def evaluate_case(
    case_id: str,
    evidence: Evidence,
    catalog: ContractCatalog,
    contract: Optional[dict[str, Any]],
    contract_problems: list[str],
    cal: Optional[Calendar],
    config: ReportConfig,
) -> dict[str, Any]:
    sources = evidence.sources_for_case(case_id)
    selected_source_record_ids = sorted(
        rid for rid, op in evidence.winners.items() if op.case_id == case_id
    )
    active_intakes = [op for op in evidence.winners.values() if op.case_id == case_id and op.op == "put" and op.kind == "intake"]
    active_completions = [op for op in evidence.winners.values() if op.case_id == case_id and op.op == "put" and op.kind == "completion"]
    active_waits = [op for op in evidence.winners.values() if op.case_id == case_id and op.op == "put" and op.kind == "waiting"]
    active_levels = [op for op in evidence.winners.values() if op.case_id == case_id and op.op == "put" and op.kind == "level_change"]

    row: dict[str, Any] = {
        "case_id": case_id,
        "issuable": False,
        "included": True,
        "closed": False,
        "timeliness": TIMELINESS_INDETERMINATE,
        "used_minutes": None,
        "known_used_minutes": 0,
        "breached": None,
        "earliest_breach": None,
        "known_breach": None,
        "intake_at": None,
        "completion_at": None,
        "observation_at": config.as_of_text,
        "domain_end_at": None,
        "service_point": None,
        "contract_id": contract.get("contract_id") if contract else None,
        "contract_catalog": {"catalog_id": catalog.catalog_id, "version": catalog.version},
        "calendar": calendar_ref(cal),
        "level_at_intake": None,
        "current_level": None,
        "quota_minutes": None,
        "blocking_reasons": list(contract_problems),
        "coverage_gaps": [],
        "effective_wait_intervals": [],
        "billable_intervals": [],
        "level_timeline": [],
        "selected_source_record_ids": selected_source_record_ids,
        "sources": sources,
    }

    if len(active_intakes) == 0:
        revoked = any(op.record_id in evidence.winners and evidence.winners[op.record_id].op == "revoke"
                      for op in evidence.ops if op.case_id == case_id and op.kind == "intake")
        row["blocking_reasons"].append("intake_revoked_or_missing" if revoked else "no_active_intake")
        return row
    if len(active_intakes) > 1:
        row["blocking_reasons"].append("ambiguous_intake_records:" + ",".join(op.record_id for op in active_intakes))
        return row

    intake = active_intakes[0]
    start = intake.at
    assert start is not None
    row["intake_at"] = format_minute(start)
    row["service_point"] = intake.service_point
    row["level_at_intake"] = intake.level
    row["current_level"] = intake.level

    if config.as_of < start:
        row["included"] = False
        row["issuable"] = True
        row["timeliness"] = "not_started_before_observation"
        row["blocking_reasons"] = []
        row["domain_end_at"] = format_minute(start)
        return row

    # Completion is only a fact closing this observation if it is <= as_of.
    valid_completions: list[Op] = []
    for comp in active_completions:
        if comp.at is None or comp.at < start:
            row["blocking_reasons"].append(
                f"completion_before_intake:{comp.record_id}@{format_minute(comp.at)}"
            )
        else:
            valid_completions.append(comp)
    if len(active_completions) > 1:
        row["blocking_reasons"].append("ambiguous_completion_records:" + ",".join(op.record_id for op in active_completions))

    completion: Optional[Op] = None
    if len(valid_completions) == 1 and not any(x.startswith("completion_before_intake") for x in row["blocking_reasons"]):
        candidate = valid_completions[0]
        assert candidate.at is not None
        if candidate.at <= config.as_of:
            completion = candidate
            row["closed"] = True
            row["completion_at"] = format_minute(candidate.at)
            end = candidate.at
        else:
            end = config.as_of
            row["completion_at"] = format_minute(candidate.at)
            row["completion_observed_after_as_of"] = True
    else:
        end = config.as_of

    if any(x.startswith("completion_before_intake") for x in row["blocking_reasons"]):
        row["domain_end_at"] = format_minute(end if 'end' in locals() else config.as_of)
        return row
    if len(active_completions) > 1:
        # Ambiguous completion prevents a trustworthy endpoint.
        end = config.as_of
        row["closed"] = False

    end_is_completion = completion is not None and end == completion.at
    row["domain_end_at"] = format_minute(end)

    # Effective waiting intervals clipped to the half-open service domain.
    wait_ivs: list[tuple[int, int]] = []
    for wait in active_waits:
        ws = wait.start
        if ws is None:
            continue
        raw_end = wait.end if wait.end is not None else end
        we = min(raw_end, end)
        ws2 = max(ws, start)
        if we > ws2:
            wait_ivs.append((ws2, we))
    waits = merge_intervals(wait_ivs)

    # Coverage gaps matter only when the clock would not already be stopped.
    nonwaiting = subtract_interval(start, end, waits)
    coverage = cal.coverage if cal else []
    gaps: list[tuple[int, int]] = []
    for seg_s, seg_e in nonwaiting:
        gaps.extend(subtract_interval(seg_s, seg_e, coverage))
    if gaps:
        row["coverage_gaps"] = [interval_json(g) for g in gaps]
        if cal is None:
            row["blocking_reasons"].append(
                f"calendar_not_found_for_service_point:{intake.service_point}"
            )
        else:
            row["blocking_reasons"].append("calendar_coverage_gap")
    # If there are no non-waiting minutes, the calendar cannot change used
    # minutes; a missing calendar therefore does not block that case.

    row["effective_wait_intervals"] = [interval_json(w) for w in waits]
    covered_open = intersect_intervals(cal.open_union if cal else [], coverage)
    domain_open = intersect_intervals(covered_open, [(start, end)])
    billable_intervals: list[tuple[int, int]] = []
    for open_s, open_e in domain_open:
        billable_intervals.extend(subtract_interval(open_s, open_e, waits))
    row["billable_intervals"] = [interval_json(iv) for iv in merge_intervals(billable_intervals)]

    # Resolve initial quota/level.
    level = intake.level
    quota, quota_error = _quota_for_level(contract, level)
    quotas_valid = quota_error is None
    if quota_error:
        row["blocking_reasons"].append(quota_error)
    row["quota_minutes"] = quota

    events = [e for e in active_levels if e.at is not None and start <= e.at <= end]
    events.sort(key=lambda e: (e.at, e.business_seq, e.submit_no, e.op_index, e.version))  # type: ignore[arg-type]
    events_by_time: dict[int, list[Op]] = {}
    for ev in events:
        events_by_time.setdefault(ev.at, []).append(ev)  # type: ignore[index]

    points = {start, end}
    for s, e in waits:
        if start <= s <= end:
            points.add(s)
        if start <= e <= end:
            points.add(e)
    for s, e in coverage:
        s2, e2 = max(s, start), min(e, end)
        if s2 < e2:
            points.update((s2, e2))
    if cal:
        for o in cal.open_intervals:
            s2, e2 = max(o["start"], start), min(o["end"], end)
            if s2 < e2:
                points.update((s2, e2))
    for ev in events:
        points.add(ev.at)  # type: ignore[arg-type]
    sorted_points = sorted(p for p in points if start <= p <= end)

    used = 0
    known_breach: Optional[dict[str, Any]] = None
    wait_starts = [item[0] for item in waits]
    coverage_starts = [item[0] for item in coverage]
    open_starts = [item[0] for item in cal.open_union] if cal else []

    def apply_level_event(ev: Op, at_start: bool = False) -> None:
        nonlocal level, quota, quotas_valid, known_breach
        level = ev.to_level or level
        q, err = _quota_for_level(contract, level)
        timeline_item = {
            "at": format_minute(ev.at),
            "to_level": level,
            "quota_minutes": q,
            "used_minutes_at_boundary": used,
            "source_record_id": ev.record_id,
            "submit_no": ev.submit_no,
            "version": ev.version,
            "business_seq": ev.business_seq,
            "applied": True,
        }
        if err:
            quotas_valid = False
            row["blocking_reasons"].append(err)
            timeline_item["quota_error"] = err
        quota = q
        row["level_timeline"].append(timeline_item)
        if known_breach is None and quotas_valid and quota is not None and used > quota:
            known_breach = {
                "at": format_minute(ev.at),
                "minute_boundary": ev.at,
                "kind": "level_change",
                "level_after_change": level,
                "quota_minutes": quota,
                "used_minutes_at_boundary": used,
                "source_record_id": ev.record_id,
                "source_submit_no": ev.submit_no,
                "source_version": ev.version,
                "business_seq": ev.business_seq,
            }

    # Level events exactly at intake apply before any minute unless this is a
    # zero-minute case that also completes on that same boundary.
    if start < end:
        for ev in events_by_time.get(start, []):
            apply_level_event(ev, at_start=True)

    for a, b in zip(sorted_points, sorted_points[1:]):
        if b <= a:
            continue
        waited = contains_with_starts(waits, wait_starts, a)
        covered = contains_with_starts(coverage, coverage_starts, a)
        is_open = bool(cal and contains_with_starts(cal.open_union, open_starts, a))
        billable = (not waited) and covered and is_open

        if billable:
            length = b - a
            if known_breach is None and quotas_valid and quota is not None and used <= quota:
                minute_offset = quota - used + 1
                if 1 <= minute_offset <= length:
                    breach_minute_boundary = a + minute_offset
                    minute_start = breach_minute_boundary - 1
                    known_breach = {
                        "at": format_minute(breach_minute_boundary),
                        "minute_boundary": breach_minute_boundary,
                        "kind": "counted_minute_right_boundary",
                        "counted_minute_start": format_minute(minute_start),
                        "counted_minute_end": format_minute(breach_minute_boundary),
                        "level_during_minute": level,
                        "quota_minutes": quota,
                        "used_minutes_after_increment": used + minute_offset,
                        "calendar_id": cal.calendar_id if cal else None,
                        "calendar_version": cal.version if cal else None,
                        "open_interval_id": _find_open_interval_id(cal, minute_start),
                        "intake_record_id": intake.record_id,
                    }
            used += length

        # At b, the just-finished minute has already been credited.  Level
        # events at a completion boundary are expressly not applied.
        apply_events_at_b = (b < end) or (b == end and not end_is_completion)
        if apply_events_at_b:
            for ev in events_by_time.get(b, []):
                apply_level_event(ev)
        elif b == end and end_is_completion:
            for ev in events_by_time.get(b, []):
                row["level_timeline"].append({
                    "at": format_minute(ev.at),
                    "to_level": ev.to_level,
                    "source_record_id": ev.record_id,
                    "submit_no": ev.submit_no,
                    "version": ev.version,
                    "business_seq": ev.business_seq,
                    "applied": False,
                    "reason": "level_change_at_completion_boundary_not_applied",
                })

    if start == end and end_is_completion:
        for ev in events_by_time.get(end, []):
            row["level_timeline"].append({
                "at": format_minute(ev.at),
                "to_level": ev.to_level,
                "source_record_id": ev.record_id,
                "submit_no": ev.submit_no,
                "version": ev.version,
                "business_seq": ev.business_seq,
                "applied": False,
                "reason": "zero_duration_completion_boundary_level_change_not_applied",
            })

    row["known_used_minutes"] = used
    row["current_level"] = level
    row["quota_minutes"] = quota if quota_error is None else None

    # De-duplicate reasons while preserving order.
    row["blocking_reasons"] = list(dict.fromkeys(row["blocking_reasons"]))
    if not row["blocking_reasons"]:
        row["issuable"] = True
        row["used_minutes"] = used
        if known_breach is not None:
            row["timeliness"] = TIMELINESS_TIMEOUT
            row["breached"] = True
            # Remove internal integer sort key before public output.
            public_breach = {k: v for k, v in known_breach.items() if k != "minute_boundary"}
            row["earliest_breach"] = public_breach
        else:
            row["timeliness"] = TIMELINESS_ON_TIME
            row["breached"] = False
    else:
        row["issuable"] = False
        row["used_minutes"] = None
        row["timeliness"] = TIMELINESS_INDETERMINATE
        row["breached"] = None
        if known_breach is not None:
            row["known_breach"] = {k: v for k, v in known_breach.items() if k != "minute_boundary"}

    return row


def generate_report(
    *,
    evidence: Evidence,
    contract_pack: dict[str, Any],
    calendar_pack: dict[str, Any],
    as_of_text: str,
    cutoff: int,
    catalog_id: str = "SLA",
    contract_version: Optional[int] = None,
    calendar_pins: Optional[dict[str, int]] = None,
    input_digests: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    as_of = parse_utc_minute(as_of_text, "as_of")
    config = ReportConfig(
        as_of=as_of,
        as_of_text=format_minute(as_of) or as_of_text,
        cutoff=cutoff,
        catalog_id=catalog_id,
        contract_version=contract_version,
        calendar_pins=calendar_pins or {},
    )
    catalog = select_contract_catalog(contract_pack, catalog_id, cutoff, contract_version)
    calendars = load_calendars(calendar_pack)

    case_ids = sorted({oid.case_id for oid in evidence.ops if oid.case_id})
    rows: list[dict[str, Any]] = []
    selected_calendar_refs: dict[str, Any] = {}

    for case_id in case_ids:
        intake_winners = [
            op for rid, op in evidence.winners.items()
            if op.case_id == case_id and op.op == "put" and op.kind == "intake"
        ]
        contract: Optional[dict[str, Any]] = None
        contract_problems: list[str] = []
        sp: Optional[str] = None
        if len(intake_winners) == 1:
            sp = intake_winners[0].service_point
            contract, contract_problems = resolve_contract(catalog, intake_winners[0])
        elif len(intake_winners) == 0:
            contract_problems = ["intake_revoked_or_missing_cannot_bind_contract"]
        else:
            contract_problems = ["ambiguous_intake_records_cannot_bind_contract"]

        cal = select_calendar(calendars, sp, cutoff, config.calendar_pins) if sp else None
        if sp:
            selected_calendar_refs[sp] = calendar_ref(cal)
        rows.append(evaluate_case(case_id, evidence, catalog, contract, contract_problems, cal, config))

    summary = {
        "total_case_records": len(rows),
        "issuable": sum(1 for r in rows if r["issuable"]),
        "closed": sum(1 for r in rows if r["closed"]),
        TIMELINESS_ON_TIME: sum(1 for r in rows if r["timeliness"] == TIMELINESS_ON_TIME),
        TIMELINESS_TIMEOUT: sum(1 for r in rows if r["timeliness"] == TIMELINESS_TIMEOUT),
        TIMELINESS_INDETERMINATE: sum(1 for r in rows if r["timeliness"] == TIMELINESS_INDETERMINATE),
        "not_started_before_observation": sum(1 for r in rows if r["timeliness"] == "not_started_before_observation"),
    }

    orphan_refs = [
        op_ref(op, evidence.winners.get(op.record_id) is op)
        for op in evidence.ops
        if not op.case_id
    ]
    evidence_issues: list[dict[str, Any]] = []
    if orphan_refs:
        evidence_issues.append({
            "code": "records_without_case_id_not_evaluated",
            "records": orphan_refs,
        })

    return {
        "report_type": "restatable_sla_time_limit_report",
        "schema_version": SCHEMA_VERSION,
        "parameters": {
            "observation_as_of": config.as_of_text,
            "evidence_cutoff_submit_no": cutoff,
            "contract_catalog_id": catalog_id,
            "contract_catalog_version_pin": contract_version,
            "calendar_version_pins": config.calendar_pins,
        },
        "input_digests": input_digests or {},
        "contract_catalog": {
            "catalog_id": catalog.catalog_id,
            "version": catalog.version,
            "visible_from_submit_no": catalog.visible_from_submit_no,
            "digest": canonical_digest(catalog.raw),
        },
        "selected_calendars": selected_calendar_refs,
        "evidence_issues": evidence_issues,
        "summary": summary,
        "cases": rows,
    }


# ---------------------------------------------------------------------------
# CSV / restatement
# ---------------------------------------------------------------------------


CSV_FIELDS = [
    "case_id", "issuable", "included", "closed", "timeliness", "used_minutes",
    "known_used_minutes", "breached", "service_point", "contract_id",
    "contract_catalog_version", "calendar_id", "calendar_version",
    "current_level", "quota_minutes", "earliest_breach_at", "breach_kind",
    "blocking_reasons", "coverage_gaps", "source_record_ids",
]


def report_to_csv(report: dict[str, Any], path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in report["cases"]:
            breach = row.get("earliest_breach") or row.get("known_breach") or {}
            cal = row.get("calendar") or {}
            source_ids = []
            for src in row.get("sources", []):
                label = f"{src['record_id']}@v{src['version']}/sub{src['submit_no']}"
                if src.get("selected_as_highest_visible_version"):
                    label += "*"
                source_ids.append(label)
            writer.writerow({
                "case_id": row["case_id"],
                "issuable": row["issuable"],
                "included": row["included"],
                "closed": row["closed"],
                "timeliness": row["timeliness"],
                "used_minutes": "" if row["used_minutes"] is None else row["used_minutes"],
                "known_used_minutes": row["known_used_minutes"],
                "breached": "" if row["breached"] is None else row["breached"],
                "service_point": row.get("service_point") or "",
                "contract_id": row.get("contract_id") or "",
                "contract_catalog_version": row["contract_catalog"]["version"],
                "calendar_id": cal.get("calendar_id") or "",
                "calendar_version": cal.get("version") or "",
                "current_level": row.get("current_level") or "",
                "quota_minutes": "" if row.get("quota_minutes") is None else row["quota_minutes"],
                "earliest_breach_at": breach.get("at", ""),
                "breach_kind": breach.get("kind", ""),
                "blocking_reasons": ";".join(row.get("blocking_reasons", [])),
                "coverage_gaps": ";".join(f"{g['start']}/{g['minutes']}m" for g in row.get("coverage_gaps", [])),
                "source_record_ids": ";".join(source_ids),
            })


def changes_to_csv(comparison: dict[str, Any], path: str | Path) -> None:
    fields = [
        "case_id", "transition", "changed_fields",
        "before_closed", "after_closed",
        "before_issuable", "after_issuable",
        "before_timeliness", "after_timeliness",
        "before_used_minutes", "after_used_minutes",
        "before_earliest_breach_at", "after_earliest_breach_at",
        "before_calendar_version", "after_calendar_version",
        "added_sources", "removed_sources",
    ]
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in comparison.get("changed_cases", []):
            before = item["before"]
            after = item["after"]
            writer.writerow({
                "case_id": item["case_id"],
                "transition": item["transition"],
                "changed_fields": ";".join(item["changed_fields"]),
                "before_closed": before.get("closed", ""),
                "after_closed": after.get("closed", ""),
                "before_issuable": before.get("issuable", ""),
                "after_issuable": after.get("issuable", ""),
                "before_timeliness": before.get("timeliness", ""),
                "after_timeliness": after.get("timeliness", ""),
                "before_used_minutes": before.get("used_minutes", ""),
                "after_used_minutes": after.get("used_minutes", ""),
                "before_earliest_breach_at": before.get("earliest_breach_at") or "",
                "after_earliest_breach_at": after.get("earliest_breach_at") or "",
                "before_calendar_version": before.get("calendar_version") or "",
                "after_calendar_version": after.get("calendar_version") or "",
                "added_sources": ";".join(
                    f"{s.get('record_id')}@v{s.get('version')}/sub{s.get('submit_no')}"
                    for s in item.get("added_source_refs", [])
                ),
                "removed_sources": ";".join(
                    f"{s.get('record_id')}@v{s.get('version')}/sub{s.get('submit_no')}"
                    for s in item.get("removed_source_refs", [])
                ),
            })


def _comparison_snapshot(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if row is None:
        return {"present": False}
    breach = row.get("earliest_breach")
    return {
        "present": True,
        "closed": row.get("closed"),
        "issuable": row.get("issuable"),
        "timeliness": row.get("timeliness"),
        "used_minutes": row.get("used_minutes"),
        "known_used_minutes": row.get("known_used_minutes"),
        "breached": row.get("breached"),
        "earliest_breach_at": (breach or {}).get("at"),
        "blocking_reasons": row.get("blocking_reasons", []),
        "calendar_version": (row.get("calendar") or {}).get("version"),
        "contract_catalog_version": (row.get("contract_catalog") or {}).get("version"),
    }


def _source_key(src: dict[str, Any]) -> str:
    return f"{src.get('source_type')}:{src.get('record_id')}:v{src.get('version')}:sub{src.get('submit_no')}:{src.get('op')}"


def compare_reports(original: dict[str, Any], restated: dict[str, Any]) -> dict[str, Any]:
    old_rows = {r["case_id"]: r for r in original.get("cases", [])}
    new_rows = {r["case_id"]: r for r in restated.get("cases", [])}
    changed: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for cid in sorted(set(old_rows) | set(new_rows)):
        old = old_rows.get(cid)
        new = new_rows.get(cid)
        before = _comparison_snapshot(old)
        after = _comparison_snapshot(new)
        changed_fields = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))

        old_sources = {_source_key(s): s for s in (old or {}).get("sources", [])}
        new_sources = {_source_key(s): s for s in (new or {}).get("sources", [])}
        added_sources = [new_sources[k] for k in sorted(set(new_sources) - set(old_sources))]
        removed_sources = [old_sources[k] for k in sorted(set(old_sources) - set(new_sources))]
        source_changed = bool(added_sources or removed_sources)

        if not changed_fields and not source_changed:
            continue

        transition = f"{before.get('timeliness', 'absent')}->{after.get('timeliness', 'absent')}"
        counts[transition] = counts.get(transition, 0) + 1
        configuration_changes = {
            key: {"before": before.get(key), "after": after.get(key)}
            for key in ("calendar_version", "contract_catalog_version")
            if before.get(key) != after.get(key)
        }
        changed.append({
            "case_id": cid,
            "transition": transition,
            "changed_fields": changed_fields,
            "configuration_changes": configuration_changes,
            "before": before,
            "after": after,
            "added_source_refs": [_brief_source(s) for s in added_sources],
            "removed_source_refs": [_brief_source(s) for s in removed_sources],
            "restated_sources": (new or {}).get("sources", []),
        })

    status_flips = [
        c for c in changed
        if c["before"].get("timeliness") in {TIMELINESS_ON_TIME, TIMELINESS_TIMEOUT}
        and c["after"].get("timeliness") in {TIMELINESS_ON_TIME, TIMELINESS_TIMEOUT}
        and c["before"].get("timeliness") != c["after"].get("timeliness")
    ]
    return {
        "changed_case_count": len(changed),
        "status_flip_count": len(status_flips),
        "transition_counts": counts,
        "status_flips": [
            {"case_id": c["case_id"], "transition": c["transition"],
             "configuration_changes": c["configuration_changes"],
             "before": c["before"], "after": c["after"],
             "added_source_refs": c["added_source_refs"],
             "removed_source_refs": c["removed_source_refs"]}
            for c in status_flips
        ],
        "changed_cases": changed,
    }


def _brief_source(src: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": src.get("record_id"),
        "kind": src.get("kind"),
        "version": src.get("version"),
        "op": src.get("op"),
        "submit_no": src.get("submit_no"),
        "selected_as_highest_visible_version": src.get("selected_as_highest_visible_version"),
        "effective_state_as_of_cutoff": src.get("effective_state_as_of_cutoff"),
    }


# ---------------------------------------------------------------------------
# Signed close envelope
# ---------------------------------------------------------------------------


def make_signed_close(report: dict[str, Any], close_id: str, signing_key: Optional[str]) -> dict[str, Any]:
    digest = canonical_digest(report)
    envelope: dict[str, Any] = {
        "envelope_type": "signed_daily_close",
        "schema_version": SCHEMA_VERSION,
        "close_id": close_id,
        "original_observation_as_of": report["parameters"]["observation_as_of"],
        "original_evidence_cutoff_submit_no": report["parameters"]["evidence_cutoff_submit_no"],
        "payload": report,
        "payload_sha256": digest,
        "seal_algorithm": "hmac-sha256" if signing_key else "content-sha256",
    }
    if signing_key:
        mac = hmac.new(signing_key.encode("utf-8"), digest.encode("ascii"), hashlib.sha256).hexdigest()
        envelope["seal"] = "hmac-sha256:" + mac
    else:
        envelope["seal"] = digest
    return envelope


def verify_signed_close(envelope: dict[str, Any]) -> dict[str, Any]:
    payload = envelope.get("payload")
    expected = envelope.get("payload_sha256")
    actual = canonical_digest(payload) if isinstance(payload, dict) else None
    digest_ok = hmac.compare_digest(actual or "", expected or "")
    key = os.environ.get("SLA_SIGNING_KEY")
    seal_ok = True
    if envelope.get("seal_algorithm") == "hmac-sha256":
        if not key:
            seal_ok = False
        else:
            mac = hmac.new(key.encode("utf-8"), (expected or "").encode("ascii"), hashlib.sha256).hexdigest()
            seal_ok = hmac.compare_digest("hmac-sha256:" + mac, envelope.get("seal", ""))
    return {"digest_valid": digest_ok, "seal_valid": seal_ok, "integrity_valid": digest_ok and seal_ok}


# ---------------------------------------------------------------------------
# Reproducible sample
# ---------------------------------------------------------------------------


def sample_data() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "description": "Small UTC-minute sample with revisions, overlapping waits, levels and missing calendar.",
        "submissions": [
            {
                "submit_no": 1,
                "records": [
                    {"op": "put", "version": 1, "kind": "intake", "record_id": "I-WAIT-EXACT", "case_id": "C-WAIT-EXACT", "at": "2026-01-05T09:00Z", "service_point": "EAST", "level": "STD"},
                    {"op": "put", "version": 1, "kind": "intake", "record_id": "I-RECOVERY", "case_id": "C-RECOVERY", "at": "2026-01-05T09:00Z", "service_point": "EAST", "level": "STD"},
                    {"op": "put", "version": 1, "kind": "intake", "record_id": "I-LEVEL", "case_id": "C-LEVEL-DOWN-UP", "at": "2026-01-05T09:00Z", "service_point": "EAST", "level": "STD"},
                    {"op": "put", "version": 1, "kind": "intake", "record_id": "I-COMP-LEVEL", "case_id": "C-COMPLETION-LEVEL", "at": "2026-01-05T13:00Z", "service_point": "EAST", "level": "STD"},
                    {"op": "put", "version": 1, "kind": "intake", "record_id": "I-REVOKE", "case_id": "C-REVOKED-WAIT", "at": "2026-01-05T09:00Z", "service_point": "EAST", "level": "STD"},
                    {"op": "put", "version": 1, "kind": "intake", "record_id": "I-REPLACE", "case_id": "C-REPLACED-INTAKE", "at": "2026-01-05T09:00Z", "service_point": "EAST", "level": "STD"},
                    {"op": "put", "version": 1, "kind": "intake", "record_id": "I-CAL", "case_id": "C-CALENDAR-FIX", "at": "2026-01-05T09:00Z", "service_point": "WEST", "level": "EXTENDED"},
                    {"op": "put", "version": 1, "kind": "intake", "record_id": "I-NOCAL", "case_id": "C-NO-CALENDAR", "at": "2026-01-05T09:00Z", "service_point": "NORTH", "level": "STD"},
                    {"op": "put", "version": 1, "kind": "intake", "record_id": "I-YEAR", "case_id": "C-YEAR-BOUNDARY", "at": "2025-12-31T23:00Z", "service_point": "EAST", "level": "STD"},
                ],
            },
            {
                "submit_no": 2,
                "records": [
                    {"op": "put", "version": 1, "kind": "completion", "record_id": "X-WAIT-EXACT", "case_id": "C-WAIT-EXACT", "at": "2026-01-05T12:00Z"},
                    {"op": "put", "version": 1, "kind": "completion", "record_id": "X-RECOVERY", "case_id": "C-RECOVERY", "at": "2026-01-05T12:00Z"},
                    {"op": "put", "version": 1, "kind": "completion", "record_id": "X-LEVEL", "case_id": "C-LEVEL-DOWN-UP", "at": "2026-01-05T17:00Z"},
                    {"op": "put", "version": 1, "kind": "completion", "record_id": "X-COMP-LEVEL", "case_id": "C-COMPLETION-LEVEL", "at": "2026-01-05T15:00Z"},
                    {"op": "put", "version": 1, "kind": "completion", "record_id": "X-REVOKE", "case_id": "C-REVOKED-WAIT", "at": "2026-01-05T12:00Z"},
                    {"op": "put", "version": 1, "kind": "completion", "record_id": "X-REPLACE", "case_id": "C-REPLACED-INTAKE", "at": "2026-01-05T12:00Z"},
                    {"op": "put", "version": 1, "kind": "completion", "record_id": "X-CAL", "case_id": "C-CALENDAR-FIX", "at": "2026-01-05T14:00Z"},
                    {"op": "put", "version": 1, "kind": "completion", "record_id": "X-NOCAL", "case_id": "C-NO-CALENDAR", "at": "2026-01-05T17:00Z"},
                    {"op": "put", "version": 1, "kind": "completion", "record_id": "X-YEAR", "case_id": "C-YEAR-BOUNDARY", "at": "2026-01-01T01:00Z"},
                ],
            },
            {
                "submit_no": 3,
                "records": [
                    {"op": "put", "version": 1, "kind": "waiting", "record_id": "W-EXACT", "case_id": "C-WAIT-EXACT", "start": "2026-01-05T10:00Z", "end": "2026-01-05T11:00Z"},
                    {"op": "put", "version": 1, "kind": "waiting", "record_id": "W-REVOKE", "case_id": "C-REVOKED-WAIT", "start": "2026-01-05T10:00Z", "end": "2026-01-05T11:00Z"},
                    {"op": "put", "version": 1, "kind": "waiting", "record_id": "W-REC-A", "case_id": "C-RECOVERY", "start": "2026-01-05T10:00Z", "end": "2026-01-05T10:30Z"},
                    {"op": "put", "version": 1, "kind": "waiting", "record_id": "W-REC-B", "case_id": "C-RECOVERY", "start": "2026-01-05T10:20Z", "end": "2026-01-05T10:30Z"},
                    {"op": "put", "version": 1, "kind": "waiting", "record_id": "W-REC-C", "case_id": "C-RECOVERY", "start": "2026-01-05T10:31Z", "end": "2026-01-05T11:00Z"},
                ],
            },
            {
                "submit_no": 4,
                "records": [
                    {"op": "put", "version": 1, "kind": "level_change", "record_id": "L-DOWN", "case_id": "C-LEVEL-DOWN-UP", "at": "2026-01-05T11:00Z", "to_level": "PRIORITY", "business_seq": 10},
                    {"op": "put", "version": 1, "kind": "level_change", "record_id": "L-UP", "case_id": "C-LEVEL-DOWN-UP", "at": "2026-01-05T13:00Z", "to_level": "ENTERPRISE", "business_seq": 10},
                    {"op": "put", "version": 1, "kind": "level_change", "record_id": "L-COMP", "case_id": "C-COMPLETION-LEVEL", "at": "2026-01-05T15:00Z", "to_level": "PRIORITY", "business_seq": 10},
                ],
            },
            {
                "submit_no": 7,
                "records": [
                    {"op": "revoke", "version": 2, "kind": "waiting", "record_id": "W-REVOKE", "case_id": "C-REVOKED-WAIT", "reason": "erroneous wait interval; customer did not request materials"},
                ],
            },
            {
                "submit_no": 8,
                "records": [
                    {"op": "put", "version": 2, "kind": "intake", "record_id": "I-REPLACE", "case_id": "C-REPLACED-INTAKE", "at": "2026-01-05T10:00Z", "service_point": "EAST", "level": "STD", "reason": "scanner batch had wrong cover timestamp"},
                ],
            },
        ],
    }

    contracts = {
        "schema_version": SCHEMA_VERSION,
        "catalogs": [
            {
                "catalog_id": "SLA",
                "version": 1,
                "visible_from_submit_no": 1,
                "contracts": [
                    {
                        "contract_id": "STANDARD-UTC",
                        "effective_from": "2025-01-01T00:00Z",
                        "effective_to": None,
                        "service_points": ["EAST", "WEST", "NORTH"],
                        "levels": {
                            "STD": 120,
                            "PRIORITY": 60,
                            "EXTENDED": 240,
                            "ENTERPRISE": 480,
                        },
                    }
                ],
            }
        ],
    }

    calendars = {
        "schema_version": SCHEMA_VERSION,
        "calendars": [
            {
                "calendar_id": "EAST-UTC",
                "service_point": "EAST",
                "version": 1,
                "visible_from_submit_no": 1,
                "coverage": [
                    {"id": "COV-YEAR", "start": "2025-12-31T23:00Z", "end": "2026-01-01T01:00Z"},
                    {"id": "COV-JAN5", "start": "2026-01-05T00:00Z", "end": "2026-01-06T00:00Z"},
                ],
                "open_intervals": [
                    {"id": "OPEN-YEAR", "start": "2025-12-31T23:00Z", "end": "2026-01-01T01:00Z"},
                    {"id": "OPEN-JAN5-AM", "start": "2026-01-05T09:00Z", "end": "2026-01-05T12:00Z"},
                    {"id": "OPEN-JAN5-PM", "start": "2026-01-05T13:00Z", "end": "2026-01-05T17:00Z"},
                ],
            },
            {
                "calendar_id": "WEST-UTC",
                "service_point": "WEST",
                "version": 1,
                "visible_from_submit_no": 1,
                "note": "Original signed calendar erroneously treated lunch as open.",
                "coverage": [{"id": "COV-WEST-JAN5", "start": "2026-01-05T00:00Z", "end": "2026-01-06T00:00Z"}],
                "open_intervals": [{"id": "OPEN-WEST-V1-09-14", "start": "2026-01-05T09:00Z", "end": "2026-01-05T14:00Z"}],
            },
            {
                "calendar_id": "WEST-UTC",
                "service_point": "WEST",
                "version": 2,
                "visible_from_submit_no": 9,
                "note": "Corrected lunch closure 12:00-13:00.",
                "coverage": [{"id": "COV-WEST-JAN5", "start": "2026-01-05T00:00Z", "end": "2026-01-06T00:00Z"}],
                "open_intervals": [
                    {"id": "OPEN-WEST-V2-AM", "start": "2026-01-05T09:00Z", "end": "2026-01-05T12:00Z"},
                    {"id": "OPEN-WEST-V2-PM", "start": "2026-01-05T13:00Z", "end": "2026-01-05T17:00Z"},
                ],
            },
        ],
    }
    return evidence, contracts, calendars


def write_sample(directory: str | Path) -> dict[str, Path]:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    evidence, contracts, calendars = sample_data()
    submission_dir = out / "submissions"
    submission_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "evidence": out / "evidence.json",
        "submissions_dir": submission_dir,
        "contracts": out / "contracts.json",
        "calendars": out / "calendars.json",
    }
    for path, data in [
        (paths["evidence"], evidence),
        (paths["contracts"], contracts),
        (paths["calendars"], calendars),
    ]:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for submission in evidence["submissions"]:
        submit_no = int(submission["submit_no"])
        path = submission_dir / f"submission-{submit_no:04d}.json"
        path.write_text(json.dumps(submission, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return paths


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_calendar_pins(values: Optional[list[str]]) -> dict[str, int]:
    pins: dict[str, int] = {}
    for value in values or []:
        if "=" not in value:
            raise InputError(f"--calendar-version expects SERVICE_POINT=VERSION, got {value!r}")
        sp, ver = value.split("=", 1)
        pins[sp] = int(ver)
    return pins


def write_json(path: str | Path, obj: Any) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_report_from_args(args: argparse.Namespace, input_digests: Optional[dict[str, str]] = None) -> dict[str, Any]:
    evidence = load_evidence(args.evidence, args.cutoff)
    contracts = load_json_object(args.contracts)
    calendars = load_json_object(args.calendars)
    pins = parse_calendar_pins(getattr(args, "calendar_version", None))
    digests = input_digests
    if digests is None:
        digests = {
            "evidence": sha256_path(args.evidence),
            "contracts": sha256_file(args.contracts),
            "calendars": sha256_file(args.calendars),
        }
    return generate_report(
        evidence=evidence,
        contract_pack=contracts,
        calendar_pack=calendars,
        as_of_text=args.as_of,
        cutoff=args.cutoff,
        catalog_id=args.catalog_id,
        contract_version=args.contract_version,
        calendar_pins=pins,
        input_digests=digests,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline restatable SLA time-limit report")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sample = sub.add_parser("sample", help="Write a reproducible small JSON sample")
    p_sample.add_argument("--dir", required=True)

    def add_report_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--evidence", required=True, help="Evidence JSON pack, JSONL, or submission-number directory")
        p.add_argument("--contracts", required=True)
        p.add_argument("--calendars", required=True)
        p.add_argument("--as-of", required=True, help="Observation moment, e.g. 2026-01-06T00:00Z")
        p.add_argument("--cutoff", required=True, type=int, help="Inclusive visible submission number")
        p.add_argument("--catalog-id", default="SLA")
        p.add_argument("--contract-version", type=int, default=None)
        p.add_argument("--calendar-version", action="append", help="Pin SERVICE_POINT=VERSION; repeatable")
        p.add_argument("--out", required=True, help="Output JSON path")
        p.add_argument("--csv", default=None, help="Optional output CSV path")

    p_report = sub.add_parser("report", help="Generate one report")
    add_report_args(p_report)

    p_close = sub.add_parser("close", help="Generate and seal a signed daily close")
    add_report_args(p_close)
    p_close.add_argument("--close-id", required=True)

    p_restate = sub.add_parser("restate", help="Restate a signed close with same as-of and newer facts/calendar")
    p_restate.add_argument("--signed", required=True)
    p_restate.add_argument("--evidence", required=True, help="Evidence JSON pack, JSONL, or submission-number directory")
    p_restate.add_argument("--contracts", required=True)
    p_restate.add_argument("--calendars", required=True)
    p_restate.add_argument("--cutoff", type=int, default=None, help="New inclusive cutoff; defaults to signed cutoff")
    p_restate.add_argument("--catalog-id", default=None)
    p_restate.add_argument("--contract-version", type=int, default=None)
    p_restate.add_argument("--calendar-version", action="append")
    p_restate.add_argument("--out", required=True)
    p_restate.add_argument("--csv", default=None, help="Write restated case rows to this CSV")
    p_restate.add_argument("--changes-csv", default=None, help="Write before/after changed cases to this CSV")

    p_verify = sub.add_parser("verify", help="Verify a signed close digest/seal")
    p_verify.add_argument("--signed", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "sample":
            paths = write_sample(args.dir)
            print(json.dumps({"written": {k: str(v) for k, v in paths.items()}}, indent=2))
            return 0

        if args.command == "verify":
            envelope = load_json_object(args.signed)
            result = verify_signed_close(envelope)
            print(json.dumps(result, indent=2))
            return 0 if result["integrity_valid"] else 3

        if args.command in {"report", "close"}:
            if args.command == "close":
                out = Path(args.out)
                if out.exists():
                    raise InputError(
                        f"signed close already exists and must not be overwritten: {out}; "
                        "write a restatement to a new path"
                    )
            report = build_report_from_args(args)
            if args.command == "report":
                write_json(args.out, report)
            else:
                key = os.environ.get("SLA_SIGNING_KEY")
                envelope = make_signed_close(report, args.close_id, key)
                write_json(args.out, envelope)
            if args.csv:
                report_to_csv(report, args.csv)
            print_summary(report)
            return 0

        if args.command == "restate":
            envelope = load_json_object(args.signed)
            verification = verify_signed_close(envelope)
            original = envelope.get("payload")
            if not isinstance(original, dict):
                raise InputError("signed file does not contain a report payload")
            old_params = original["parameters"]

            cutoff = args.cutoff if args.cutoff is not None else old_params["evidence_cutoff_submit_no"]
            as_of = old_params["observation_as_of"]
            catalog_id = args.catalog_id or old_params["contract_catalog_id"]
            contract_version = args.contract_version
            if contract_version is None:
                # Preserve an explicit signed pin.  When the signed report used
                # auto-selection (null), the new cutoff may select a newer
                # visible catalog version according to the normal rule.
                contract_version = old_params.get("contract_catalog_version_pin")
            old_pins = old_params.get("calendar_version_pins", {})
            new_pins = parse_calendar_pins(args.calendar_version)
            # Explicit CLI pins replace same-service old pins; otherwise retain.
            merged_pins = dict(old_pins)
            merged_pins.update(new_pins)

            evidence = load_evidence(args.evidence, cutoff)
            contracts = load_json_object(args.contracts)
            calendars = load_json_object(args.calendars)
            digests = {
                "evidence": sha256_path(args.evidence),
                "contracts": sha256_file(args.contracts),
                "calendars": sha256_file(args.calendars),
                "signed_original": sha256_file(args.signed),
            }
            restated = generate_report(
                evidence=evidence,
                contract_pack=contracts,
                calendar_pack=calendars,
                as_of_text=as_of,
                cutoff=cutoff,
                catalog_id=catalog_id,
                contract_version=contract_version,
                calendar_pins=merged_pins,
                input_digests=digests,
            )
            changes = compare_reports(original, restated)
            output = {
                "restatement_type": "signed_close_side_by_side",
                "schema_version": SCHEMA_VERSION,
                "close_id": envelope.get("close_id"),
                "same_observation_as_of": as_of,
                "original_signed_file": str(Path(args.signed).resolve()),
                "original_integrity_check": verification,
                "original_payload_sha256": envelope.get("payload_sha256"),
                "restated_parameters": restated["parameters"],
                "change_summary": {
                    "changed_case_count": changes["changed_case_count"],
                    "status_flip_count": changes["status_flip_count"],
                    "transition_counts": changes["transition_counts"],
                },
                "status_flips": changes["status_flips"],
                "changes": changes,
                "original_report": original,
                "restated_report": restated,
            }
            write_json(args.out, output)
            if args.csv:
                report_to_csv(restated, args.csv)
            if args.changes_csv:
                changes_to_csv(changes, args.changes_csv)
            print(json.dumps(output["change_summary"], indent=2, ensure_ascii=False))
            return 0

        parser.error(f"unknown command {args.command}")
        return 2
    except InputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def print_summary(report: dict[str, Any]) -> None:
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
