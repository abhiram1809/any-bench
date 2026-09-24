"""Additive dataset provenance; CSV and result JSONL columns remain unchanged."""
from __future__ import annotations

import json
from pathlib import Path

from .workflow import fingerprint, private_json


def metadata_path(dataset: Path) -> Path:
    return dataset.with_name(dataset.name + ".metadata.json")


def case_metadata(case) -> dict:
    return getattr(case, "_metadata", {})


def verification_identity(case) -> str:
    return fingerprint({"case": case, "metadata": {key: value for key, value in case_metadata(case).items()
                        if key not in {"validation", "repository_alias"}}})


def attach_metadata(cases: list, dataset: Path) -> None:
    path = metadata_path(dataset)
    if not path.exists():
        if any("-group-" in case.case_id for case in cases):
            raise ValueError("Grouped dataset requires its .metadata.json sidecar")
        return
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != 1 or document.get("dataset_sha256") != fingerprint(cases):
        raise ValueError("Dataset metadata is unsupported or does not match the CSV")
    for case in cases:
        case._metadata = document.get("cases", {}).get(case.case_id, {})
        if "-group-" in case.case_id and case._metadata.get("kind") != "group":
            raise ValueError(f"Missing grouped-task provenance: {case.case_id}")


def write_metadata(cases: list, dataset: Path) -> None:
    entries = {case.case_id: case_metadata(case) for case in cases if case_metadata(case)}
    if entries:
        private_json(metadata_path(dataset), {"schema": 1, "dataset_sha256": fingerprint(cases),
                                              "cases": entries})
    else:
        metadata_path(dataset).unlink(missing_ok=True)
