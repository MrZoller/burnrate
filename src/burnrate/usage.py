"""Tolerant parsing of the unofficial /api/oauth/usage response.

This endpoint is undocumented and has already drifted from what the dashboard was
first written against: the scoped weekly bucket appears only inside `limits[]`,
identified by `scope.model.display_name`, while the `seven_day_opus` /
`seven_day_sonnet` top-level keys sit at null. A parser that trusted the
top-level keys alone would silently drop a bucket the user is actively burning.

So we read from both places and union the results:

  1. `limits[]` is the primary source. It is self-describing -- kind, group,
     percent, reset, and the scoped model's display name -- so a newly
     introduced scope shows up with a correct label and no code change.
  2. Top-level `{utilization, resets_at}` objects fill in anything `limits[]`
     did not cover, so the dashboard survives `limits` disappearing.

Both paths are best-effort per bucket: one malformed entry is recorded as a
warning and skipped, never allowed to take down the whole snapshot. Anything we
could not interpret surfaces in the UI as drift rather than vanishing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Top-level keys that are shaped like buckets but are not rate limits.
NON_BUCKET_KEYS = frozenset(
    {
        "limits",
        "extra_usage",
        "spend",
        "member_dashboard_available",
    }
)

# Friendly labels for the top-level keys we have actually seen.
KNOWN_LABELS: dict[str, str] = {
    "five_hour": "5-hour session",
    "seven_day": "Weekly (all models)",
    "seven_day_opus": "Weekly (Opus)",
    "seven_day_sonnet": "Weekly (Sonnet)",
    "seven_day_oauth_apps": "Weekly (OAuth apps)",
    "seven_day_cowork": "Weekly (Cowork)",
}

# `limits[].kind` -> the canonical key its top-level twin uses, so the two
# sources dedupe against each other.
KIND_TO_KEY: dict[str, str] = {
    "session": "five_hour",
    "weekly_all": "seven_day",
}

SESSION_GROUP = "session"
WEEKLY_GROUP = "weekly"
OTHER_GROUP = "other"

# Primary buckets sort ahead of anything we do not recognize.
_GROUP_ORDER = {SESSION_GROUP: 0, WEEKLY_GROUP: 1, OTHER_GROUP: 2}


@dataclass(frozen=True)
class Bucket:
    """One rate-limit bucket, normalized across both response shapes."""

    key: str
    label: str
    utilization: float
    resets_at: datetime | None = None
    group: str = OTHER_GROUP
    severity: str | None = None
    known: bool = True
    source: str = "limits"

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (_GROUP_ORDER.get(self.group, 3), 0 if self.known else 1, self.key)


@dataclass(frozen=True)
class UsageSnapshot:
    """Everything we could make sense of in one response."""

    buckets: tuple[Bucket, ...] = ()
    warnings: tuple[str, ...] = field(default=())
    fetched_at: datetime | None = None

    def bucket(self, key: str) -> Bucket | None:
        return next((b for b in self.buckets if b.key == key), None)

    @property
    def weekly_primary(self) -> Bucket | None:
        """The all-models weekly bucket -- what the pace projection runs on."""
        return self.bucket("seven_day")


def parse_usage(payload: Any, fetched_at: datetime | None = None) -> UsageSnapshot:
    """Turn a decoded response body into a snapshot. Never raises on bad input."""
    if not isinstance(payload, dict):
        return UsageSnapshot(
            warnings=(f"expected a JSON object, got {type(payload).__name__}",),
            fetched_at=fetched_at,
        )

    buckets: dict[str, Bucket] = {}
    warnings: list[str] = []

    _collect_from_limits(payload.get("limits"), buckets, warnings)
    _collect_from_top_level(payload, buckets, warnings)

    if not buckets:
        warnings.append("no usable buckets in response")

    ordered = tuple(sorted(buckets.values(), key=lambda b: b.sort_key))
    return UsageSnapshot(buckets=ordered, warnings=tuple(warnings), fetched_at=fetched_at)


def _collect_from_limits(limits: Any, buckets: dict[str, Bucket], warnings: list[str]) -> None:
    if limits is None:
        return
    if not isinstance(limits, list):
        warnings.append(f"limits was {type(limits).__name__}, expected a list")
        return

    for index, entry in enumerate(limits):
        if not isinstance(entry, dict):
            warnings.append(f"limits[{index}] was {type(entry).__name__}, expected an object")
            continue

        utilization = _as_percent(entry.get("percent"))
        if utilization is None:
            utilization = _as_percent(entry.get("utilization"))
        if utilization is None:
            # An entry with no usable number is not worth a warning on its own;
            # nulls are routine here.
            continue

        kind = entry.get("kind") if isinstance(entry.get("kind"), str) else None
        scope_name = _scope_model_name(entry.get("scope"))
        key, label, known = _identify_limit(kind, scope_name)
        if key is None:
            warnings.append(f"limits[{index}] had no usable kind or scope")
            continue

        resets_at, reset_warning = _parse_timestamp(entry.get("resets_at"))
        if reset_warning:
            warnings.append(f"{key}: {reset_warning}")

        severity = entry.get("severity") if isinstance(entry.get("severity"), str) else None
        buckets[key] = Bucket(
            key=key,
            label=label,
            utilization=utilization,
            resets_at=resets_at,
            group=group_for(kind, key),
            severity=severity,
            known=known,
            source="limits",
        )


def _collect_from_top_level(
    payload: dict[str, Any], buckets: dict[str, Bucket], warnings: list[str]
) -> None:
    for key, value in payload.items():
        if key in NON_BUCKET_KEYS or not isinstance(value, dict):
            continue

        utilization = _as_percent(value.get("utilization"))
        if utilization is None:
            continue
        if key in buckets:
            # limits[] already described this bucket, and does so more richly.
            continue

        resets_at, reset_warning = _parse_timestamp(value.get("resets_at"))
        if reset_warning:
            warnings.append(f"{key}: {reset_warning}")

        known = key in KNOWN_LABELS
        if not known:
            warnings.append(f"unrecognized bucket {key!r} rendered under its raw key")

        buckets[key] = Bucket(
            key=key,
            label=KNOWN_LABELS.get(key, humanize(key)),
            utilization=utilization,
            resets_at=resets_at,
            group=group_for(None, key),
            known=known,
            source="top_level",
        )


def _identify_limit(kind: str | None, scope_name: str | None) -> tuple[str | None, str, bool]:
    """Map a limits[] entry onto a canonical key, label, and known-ness."""
    if kind in KIND_TO_KEY:
        key = KIND_TO_KEY[kind]
        return key, KNOWN_LABELS.get(key, humanize(key)), True

    if kind == "weekly_scoped":
        if scope_name:
            # Derive the key the top-level twin would use, so the two dedupe.
            key = f"seven_day_{_slug(scope_name)}"
            return key, f"Weekly ({scope_name})", True
        return "seven_day_scoped", "Weekly (scoped)", False

    if kind:
        suffix = f"_{_slug(scope_name)}" if scope_name else ""
        label = humanize(kind) + (f" ({scope_name})" if scope_name else "")
        return f"{_slug(kind)}{suffix}", label, False

    return None, "", False


def group_for(kind: str | None, key: str) -> str:
    if kind == "session" or key == "five_hour":
        return SESSION_GROUP
    if (kind or "").startswith("weekly") or key.startswith("seven_day"):
        return WEEKLY_GROUP
    return OTHER_GROUP


def _scope_model_name(scope: Any) -> str | None:
    if not isinstance(scope, dict):
        return None
    model = scope.get("model")
    if not isinstance(model, dict):
        return None
    for candidate in (model.get("display_name"), model.get("id")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _as_percent(raw: Any) -> float | None:
    """Coerce a utilization/percent field to a float in [0, 100]."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int | float):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip().rstrip("%"))
        except ValueError:
            return None
    else:
        return None
    if value != value:  # NaN
        return None
    # Clamp rather than reject: an over-100 reading still means "at the cap".
    return max(0.0, min(100.0, value))


def _parse_timestamp(raw: Any) -> tuple[datetime | None, str | None]:
    """Parse an ISO-8601 reset time. Returns (value, warning)."""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, f"resets_at was {type(raw).__name__}, expected a string"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, f"unparseable resets_at {raw!r}"
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)), None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def humanize(key: str) -> str:
    return re.sub(r"[_\s]+", " ", key).strip().capitalize()
