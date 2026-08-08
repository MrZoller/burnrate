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

import math
import re
from dataclasses import dataclass, field, replace
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
    notices: tuple[str, ...] = field(default=())
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
    notices: list[str] = []

    _collect_from_limits(payload.get("limits"), buckets, warnings)
    _collect_from_top_level(payload, buckets, warnings, notices)

    if not buckets:
        warnings.append("no usable buckets in response")

    ordered = tuple(sorted(buckets.values(), key=lambda b: b.sort_key))
    return UsageSnapshot(
        buckets=ordered,
        warnings=tuple(warnings),
        notices=tuple(notices),
        fetched_at=fetched_at,
    )


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

        utilization, percent_warning = _read_percent(entry, "percent", "utilization")
        if utilization is None:
            # A missing or null number is routine and stays quiet. A number that
            # was there and could not be read is drift, and says so.
            if percent_warning:
                warnings.append(f"limits[{index}]: {percent_warning}")
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
    payload: dict[str, Any],
    buckets: dict[str, Bucket],
    warnings: list[str],
    notices: list[str],
) -> None:
    for key, value in payload.items():
        if key in NON_BUCKET_KEYS:
            continue
        if not isinstance(value, dict):
            # A bucket we know by name turning into a scalar or a list is drift,
            # and it reaches here before the utilization check ever runs, so the
            # malformed-number warning cannot catch it. Scoped to KNOWN_LABELS on
            # purpose: null is how this endpoint disables a limit and most of
            # these keys are null on any given response, while the unrecognized
            # ones come and go -- warning on those would light the banner for
            # fields the dashboard never renders.
            if key in KNOWN_LABELS and value is not None:
                warnings.append(f"{key} was {type(value).__name__}, expected an object")
            continue

        utilization, percent_warning = _read_percent(value, "utilization")
        if utilization is None:
            # Same split as the limits path: null is how this endpoint says "no
            # limit of this kind", but a non-null value we cannot read means the
            # gauge disappears, and it must not disappear quietly. Without the
            # warning a malformed bucket sitting beside one valid bucket left the
            # poll marked successful, the banner dark, and one gauge simply gone.
            if percent_warning:
                warnings.append(f"{key}: {percent_warning}")
            continue
        resets_at, reset_warning = _parse_timestamp(value.get("resets_at"))
        if reset_warning:
            warnings.append(f"{key}: {reset_warning}")

        existing = buckets.get(key)
        if existing is not None:
            # limits[] described this bucket already and does so more richly, so
            # it wins -- but only field by field. A limits entry carrying a
            # percentage and a null reset would otherwise discard a perfectly
            # good reset sitting in the top-level twin, which costs the gauge its
            # countdown and takes the weekly projection to "unavailable" for data
            # the response actually contained.
            if existing.resets_at is None and resets_at is not None:
                buckets[key] = replace(existing, resets_at=resets_at)
            continue

        known = key in KNOWN_LABELS
        if not known:
            # A notice, not a warning: the bucket renders with its own dashed
            # card and label, so it is already visible. Some of these keys are
            # permanent fixtures of the response, and routing them to the banner
            # would leave it lit forever -- training the eye to ignore the one
            # signal that means the data actually went bad.
            notices.append(f"unrecognized bucket {key!r} rendered under its raw key")

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


def _read_percent(container: dict[str, Any], *names: str) -> tuple[float | None, str | None]:
    """First readable percentage among `names`. Returns (value, warning).

    The distinction `_as_percent` alone cannot make: it answers None both for a
    field that was absent or null -- routine, this endpoint nulls out limits that
    do not apply -- and for one that was present and unreadable, which is schema
    drift. Collapsing the two is how a malformed bucket vanished silently.
    """
    present = [(name, container.get(name)) for name in names if container.get(name) is not None]
    if not present:
        return None, None
    for _, raw in present:
        value = _as_percent(raw)
        if value is not None:
            return value, None
    name, raw = present[0]
    return None, f"unreadable {name} {raw!r}"


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
    # Finite, not merely non-NaN. An infinity survives float() -- from a literal
    # Infinity or from an overflowing string like "1e999" -- and the clamp below
    # then turns it into a confident 100% or 0%. At 100% the projection reports
    # "already at the cap", which is the worst output this dashboard can produce:
    # a wrong number with no warning attached to it.
    if not math.isfinite(value):
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
