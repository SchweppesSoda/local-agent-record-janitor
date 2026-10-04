"""Opt-in, bounded Herdr runtime observations, separate from writer evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
import uuid

from .herdr_discovery import HerdrDiscoveryError
from .herdr_live_metadata import HerdrLiveMetadata, HerdrLiveMetadataError, decode_response, parse_detached_daemon, parse_live_snapshot, parse_pong
from .herdr_transport import HerdrTransportError, endpoint_identity, request_metadata

PROFILE_SECONDS = 2.0
ENDPOINT_SECONDS = 0.35
PROFILE_BYTES = 8 * 1024 * 1024
SNAPSHOT_BYTES = 2 * 1024 * 1024
PONG_BYTES = 16 * 1024


@dataclass
class RuntimeBudget:
    deadline: float = field(default_factory=lambda: time.monotonic() + PROFILE_SECONDS)
    remaining_bytes: int = PROFILE_BYTES


@dataclass(frozen=True)
class RuntimeObservation:
    metadata: HerdrLiveMetadata | None = None
    errors: tuple[str, ...] = ()
    generation_key: str | None = None
    observed_at: str | None = None
    pong_version: str | None = None
    detached_daemon_observed: bool | None = None

    def to_dict(self, session_name: str, raw_locator: str) -> dict:
        metadata = self.metadata
        return {
            "session_name": session_name, "endpoint": raw_locator, "scope": "session",
            "probe_complete": metadata is not None and not self.errors,
            "server_active": True if metadata is not None or self.pong_version is not None else None,
            "protocol": 22 if metadata is not None or self.pong_version is not None else None,
            "version": metadata.version if metadata is not None else self.pong_version,
            "detached_daemon_observed": self.detached_daemon_observed,
            "detached_observation_source": "pong_startup_self_report" if self.detached_daemon_observed is not None else None,
            "observed_at": self.observed_at,
            "endpoint_generation_observation": self.generation_key,
            "generation_atomic": False,
            "counts": dict(metadata.counts) if metadata is not None else {},
            "pane_states": [{"workspace_id": w, "tab_id": t, "pane_id": p, "agent_status": s}
                            for w, t, p, s in metadata.pane_states] if metadata is not None else [],
            "errors": list(self.errors),
        }


def probe_runtime(raw_locator: str, budget: RuntimeBudget) -> RuntimeObservation:
    maximum = min(SNAPSHOT_BYTES, budget.remaining_bytes - PONG_BYTES)
    if time.monotonic() >= budget.deadline or maximum <= 0:
        return RuntimeObservation(errors=("live_profile_budget_exhausted",))
    deadline = min(budget.deadline, time.monotonic() + ENDPOINT_SECONDS)
    attempted = False
    spent = 0
    version, detached, observed_at, generation = None, None, None, None
    try:
        identity = endpoint_identity(raw_locator)
        attempted = True
        ping_id, snapshot_id = "larj-ping-" + uuid.uuid4().hex, "larj-snapshot-" + uuid.uuid4().hex
        pong_data = request_metadata(raw_locator, "ping", ping_id, deadline=deadline, maximum=PONG_BYTES, identity=identity)
        spent += len(pong_data) + 1  # Transport also consumed the framing newline.
        pong = decode_response(pong_data, ping_id)
        version = parse_pong(pong)
        observed_at = datetime.now(timezone.utc).isoformat()
        generation = identity.generation_key
        detached = parse_detached_daemon(pong)
        data = request_metadata(raw_locator, "session.snapshot", snapshot_id, deadline=deadline, maximum=maximum, identity=identity)
        spent += len(data) + 1
        metadata = parse_live_snapshot(decode_response(data, snapshot_id), version)
        # Check after parsing too: neither two connections nor a marker prove
        # an atomic server generation. A changed parent/marker invalidates it.
        if endpoint_identity(raw_locator) != identity:
            raise HerdrTransportError("live_endpoint_changed")
        if time.monotonic() > deadline:
            raise HerdrTransportError("live_timeout")
        budget.remaining_bytes -= spent
        return RuntimeObservation(metadata, metadata.errors, identity.generation_key,
            observed_at, version, detached)
    except (HerdrTransportError, HerdrLiveMetadataError, HerdrDiscoveryError) as exc:
        code = str(exc)
    except (OSError, ValueError, RecursionError, OverflowError):
        code = "live_endpoint_unavailable"
    # A failed bounded read may have consumed its full allowance. Reserve it
    # conservatively rather than letting failed endpoints evade the byte cap.
    if attempted:
        budget.remaining_bytes -= PONG_BYTES + maximum
    return RuntimeObservation(errors=(code,), generation_key=generation, observed_at=observed_at,
                              pong_version=version, detached_daemon_observed=detached)
