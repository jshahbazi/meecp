"""Host-created monitored resource policy; never a hard-bound admission proof."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import struct


RESOURCE_POLICY = "held-group-windowed-usage-v1"
POLICY_FIELDS = ("footprint_bytes", "group_members", "cpu_ns", "interval_ns",
    "read_window_ns", "sample_gap_ns", "identity_capacity", "sample_capacity")
REPORT_FIELDS = ("reason", "sample_windows_valid", "samples", "attempted_samples",
    "max_group_window_footprint_bytes", "max_helper_footprint_bytes", "max_scope_window_footprint_bytes",
    "max_group_members", "observed_own_cpu_ns", "identities_seen", "longest_read_window_ns",
    "longest_sample_gap_ns", "timebase_numer", "timebase_denom", "lifetime_coverage_complete",
    "hard_aggregate_enforced")
REASONS = ("exited", "footprint_threshold", "process_threshold", "cpu_threshold",
    "membership_changed", "metric_unavailable", "identity_changed", "arithmetic_overflow",
    "read_window_exceeded", "sample_gap_exceeded", "identity_capacity_exceeded",
    "sample_capacity_exceeded", "control_closed", "deadline", "policy_invalid", "no_sample")
REPORT_SIZE = 8 * len(REPORT_FIELDS)
MAX_U64 = (1 << 64) - 1


class ResourcePolicyError(ValueError):
    def __init__(self):
        super().__init__("invalid_command_resource_policy")


@dataclass(frozen=True)
class ResourcePolicyV1:
    """Trusted constructor only; package/source/API policy selection is absent.

    Footprint includes helper overhead. Group membership counts include zombies
    conservatively. CPU sums observed own counters without child roll-up.
    """
    footprint_bytes: int = 256 * 1024**2
    group_members: int = 16
    cpu_ns: int = 2_000_000_000
    interval_ns: int = 5_000_000
    read_window_ns: int = 10_000_000
    sample_gap_ns: int = 100_000_000
    identity_capacity: int = 256
    sample_capacity: int = 100_000

    def __post_init__(self):
        bounds = ((1, 4 * 1024**3), (1, 64), (1, 3_600_000_000_000),
            (1_000_000, 100_000_000), (1, 100_000_000), (1, 1_000_000_000), (2, 1024), (1, 1_000_000))
        for value, (minimum, maximum) in zip(asdict(self).values(), bounds):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ResourcePolicyError()
        if self.sample_gap_ns < self.interval_ns or self.identity_capacity < self.group_members + 1:
            raise ResourcePolicyError()

    @property
    def digest(self) -> str:
        data = json.dumps([RESOURCE_POLICY, asdict(self)], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return hashlib.sha256(data).hexdigest()

    def wire(self) -> bytes:
        return struct.pack("!8Q", *(getattr(self, name) for name in POLICY_FIELDS))


@dataclass(frozen=True)
class ResourceReportV1:
    reason: str
    sample_windows_valid: bool
    samples: int
    attempted_samples: int
    max_group_window_footprint_bytes: int
    max_helper_footprint_bytes: int
    max_scope_window_footprint_bytes: int
    max_group_members: int
    observed_own_cpu_ns: int
    identities_seen: int
    longest_read_window_ns: int
    longest_sample_gap_ns: int
    timebase_numer: int
    timebase_denom: int
    lifetime_coverage_complete: bool = False
    hard_aggregate_enforced: bool = False

    @classmethod
    def from_wire(cls, data: bytes) -> ResourceReportV1:
        if type(data) is not bytes or len(data) != REPORT_SIZE:
            raise ResourcePolicyError()
        fields = dict(zip(REPORT_FIELDS, struct.unpack("!16Q", data)))
        if (fields["reason"] >= len(REASONS) or fields["sample_windows_valid"] not in (0, 1)
                or fields["lifetime_coverage_complete"] != 0 or fields["hard_aggregate_enforced"] != 0
                or fields["samples"] > fields["attempted_samples"] or fields["attempted_samples"] > 1_000_001
                or fields["max_group_members"] > 65 or fields["identities_seen"] > 1024
                or fields["timebase_numer"] > (1 << 32) - 1 or fields["timebase_denom"] > (1 << 32) - 1
                or fields["samples"] and (not fields["timebase_numer"] or not fields["timebase_denom"])
                or fields["sample_windows_valid"] and (not fields["samples"] or fields["samples"] != fields["attempted_samples"])
                or fields["sample_windows_valid"] and fields["reason"] in {*range(4, 12), 14, 15}
                or not fields["samples"] and any(fields[key] for key in ("max_group_window_footprint_bytes",
                    "max_helper_footprint_bytes", "max_scope_window_footprint_bytes"))
                or fields["max_scope_window_footprint_bytes"] < max(fields["max_group_window_footprint_bytes"], fields["max_helper_footprint_bytes"])):
            raise ResourcePolicyError()
        fields["reason"] = REASONS[fields["reason"]]
        for name in ("sample_windows_valid", "lifetime_coverage_complete", "hard_aggregate_enforced"):
            fields[name] = bool(fields[name])
        return cls(**fields)

    @property
    def result_admissible(self) -> bool:
        # Stable bounded windows cannot prove coverage of unseen descendants.
        return False
