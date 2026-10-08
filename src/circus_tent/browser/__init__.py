"""Browser shards: fingerprints, lifecycle, cluster, target rate limiting."""

from circus_tent.browser.cluster_manager import ClusterManager, ShardSnapshot
from circus_tent.browser.fingerprint import (
    FingerprintManifest,
    build_launch_options,
    capture_fingerprint,
    compare_fingerprints,
    fingerprint_matches,
    resolve_manifest,
)
from circus_tent.browser.rate_limiter import BackoffState, RateLimited, TargetRateLimiter
from circus_tent.browser.shard import Shard, ShardHealth, ShardState, TabContext

__all__ = [
    "BackoffState",
    "ClusterManager",
    "FingerprintManifest",
    "RateLimited",
    "Shard",
    "ShardHealth",
    "ShardSnapshot",
    "ShardState",
    "TabContext",
    "TargetRateLimiter",
    "build_launch_options",
    "capture_fingerprint",
    "compare_fingerprints",
    "fingerprint_matches",
    "resolve_manifest",
]
