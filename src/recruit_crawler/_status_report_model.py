from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict, Union

JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[JsonScalar, list["JsonValue"], dict[str, "JsonValue"]]


class FeatureRecord(TypedDict, total=False):
    feature_id: str
    title: str
    status: str
    summary: str
    current_behavior: str
    remaining_gap: str
    recommended_next: str
    code_refs: list[str]
    test_refs: list[str]
    docs_refs: list[str]
    entrypoints: list[str]


class FeatureLedger(TypedDict):
    status_date: str
    features: list[FeatureRecord]


class SourceRow(TypedDict, total=False):
    source_id: str
    target_status: str
    target_lane: str | None
    enabled: bool
    docs_refs: list[str]
    test_refs: list[str]


@dataclass(frozen=True)
class StatusReportCheck:
    ok: bool
    message: str


@dataclass(frozen=True)
class ProgressBrief:
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def feature_records(feature_ledger: FeatureLedger) -> list[FeatureRecord]:
    features = feature_ledger["features"]
    if not isinstance(features, list):
        raise TypeError("feature ledger was not loaded through load_feature_ledger")
    records: list[FeatureRecord] = []
    for feature in features:
        if not isinstance(feature, dict):
            raise TypeError("feature ledger was not loaded through load_feature_ledger")
        records.append(feature)
    return records


def string_list(record: FeatureRecord | SourceRow, field: str) -> list[str]:
    values = record.get(field)
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def text_field(record: FeatureRecord | SourceRow, field: str, fallback: str = "") -> str:
    value = record.get(field)
    if value is None:
        return fallback
    return str(value)
