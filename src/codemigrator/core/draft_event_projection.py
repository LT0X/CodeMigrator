"""Closed public projection for user-facing Draft revision events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

_ARTIFACT_NAMES = frozenset(
    {"spec", "understanding_dossier", "target_project_blueprint", "migration_rulebook"}
)
_OPTION_FIELDS = frozenset({"key", "label", "impact", "recommended"})
_SNAPSHOT_FIELDS = frozenset({"name", "version", "sha256", "size", "media_type"})


def project_draft_public_event(
    event_type: str, data: Mapping[str, object]
) -> dict[str, object] | None:
    """Allowlist the Draft event payloads that the Web may render."""

    if event_type == "session.question.asked":
        allowed = {"question_id", "revision", "prompt", "options", "allow_free_text"}
        projected = {key: data[key] for key in allowed if key in data}
        question_id = projected.get("question_id")
        revision = projected.get("revision")
        prompt = projected.get("prompt")
        options = projected.get("options")
        allow_free_text = projected.get("allow_free_text")
        if (
            not _canonical_uuid(question_id)
            or not _positive_int(revision)
            or not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > 4096
            or not isinstance(options, (list, tuple))
            or not 2 <= len(options) <= 3
            or type(allow_free_text) is not bool
        ):
            raise ValueError("Draft question event summary is invalid")
        keys: set[str] = set()
        recommended_count = 0
        clean_options: list[dict[str, object]] = []
        for option in options:
            if not isinstance(option, Mapping):
                raise ValueError("Draft question event summary is invalid")
            key, label, impact, recommended = (
                option.get("key"),
                option.get("label"),
                option.get("impact"),
                option.get("recommended"),
            )
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 64
                or key in keys
                or not isinstance(label, str)
                or not label.strip()
                or len(label) > 256
                or not isinstance(impact, str)
                or not impact.strip()
                or len(impact) > 1024
                or type(recommended) is not bool
            ):
                raise ValueError("Draft question event summary is invalid")
            keys.add(key)
            recommended_count += int(recommended)
            clean_options.append({key: option[key] for key in _OPTION_FIELDS})
        if recommended_count != 1:
            raise ValueError("Draft question event summary is invalid")
        projected["options"] = clean_options
        return projected

    if event_type == "session.draft_revision.created":
        allowed = {"revision", "artifacts", "artifact_snapshots"}
        projected = {key: data[key] for key in allowed if key in data}
        revision = projected.get("revision")
        artifacts = projected.get("artifacts")
        snapshots = projected.get("artifact_snapshots")
        if (
            not _positive_int(revision)
            or not isinstance(artifacts, Mapping)
            or set(artifacts) != _ARTIFACT_NAMES
            or any(not isinstance(artifacts[name], Mapping) for name in _ARTIFACT_NAMES)
            or not isinstance(snapshots, (list, tuple))
            or len(snapshots) != len(_ARTIFACT_NAMES)
        ):
            raise ValueError("Draft revision event summary is invalid")
        projected["artifacts"] = {
            name: _project_artifact_content_names(
                cast(Mapping[str, object], artifacts[name])
            )
            for name in sorted(_ARTIFACT_NAMES)
        }
        clean_snapshots: list[dict[str, object]] = []
        snapshot_names: set[str] = set()
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping):
                raise ValueError("Draft revision event summary is invalid")
            name = snapshot.get("name")
            if (
                not isinstance(name, str)
                or name not in _ARTIFACT_NAMES
                or name in snapshot_names
                or not _positive_int(snapshot.get("version"))
                or not isinstance(snapshot.get("sha256"), str)
                or len(cast(str, snapshot["sha256"])) != 64
                or any(char not in "0123456789abcdef" for char in cast(str, snapshot["sha256"]))
                or not _nonnegative_int(snapshot.get("size"))
                or not isinstance(snapshot.get("media_type"), str)
                or not cast(str, snapshot["media_type"]).strip()
                or len(cast(str, snapshot["media_type"])) > 256
            ):
                raise ValueError("Draft revision event summary is invalid")
            snapshot_names.add(name)
            clean_snapshots.append({key: snapshot[key] for key in _SNAPSHOT_FIELDS})
        if snapshot_names != _ARTIFACT_NAMES:
            raise ValueError("Draft revision event summary is invalid")
        projected["artifact_snapshots"] = clean_snapshots
        return projected

    if event_type in {
        "session.draft_revision.confirmation_requested",
        "session.draft_revision.confirmed",
    }:
        revision = data.get("revision")
        if not _positive_int(revision):
            raise ValueError("Draft confirmation event summary is invalid")
        return {"revision": revision}

    return None


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _project_artifact_content_names(value: Mapping[str, object]) -> dict[str, object]:
    """Expose reviewed artifact prose under a user-facing key, never generic content."""

    projected: dict[str, object] = {}
    for key, nested in value.items():
        public_key = "text" if key == "content" else key
        if public_key in projected:
            raise ValueError("Draft artifact event contains colliding public fields")
        if isinstance(nested, Mapping):
            projected[public_key] = _project_artifact_content_names(nested)
        elif isinstance(nested, (list, tuple)):
            projected[public_key] = [
                _project_artifact_content_names(item)
                if isinstance(item, Mapping)
                else item
                for item in nested
            ]
        else:
            projected[public_key] = nested
    return projected


__all__ = ["project_draft_public_event"]
