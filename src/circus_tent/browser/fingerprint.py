"""Fingerprint manifest pinning + in-page fingerprint hash capture.

See modules/browser/.omp-spec.md. Fingerprints are IMMUTABLE per profile: the
manifest is written once at first initialization and never overwritten.
Changing a fingerprint requires purging the profile directory and re-running
MFA bootstrap (ARCHITECTURE §5.2).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "fingerprint.json"

#: In-page fingerprint probe. Returns JSON {canvas, webgl, audio, navigator,
#: screen, fonts} with FNV-1a hashes for canvas/webgl/audio and enumerated
#: values for the rest. Deterministic for the same profile across launches
#: (no randomness anywhere in the probe).
FINGERPRINT_JS: str = """
() => {
  const fnv = (str) => {
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return (h >>> 0).toString(16);
  };
  let canvas = "";
  try {
    const c = document.createElement("canvas");
    c.width = 220; c.height = 30;
    const ctx = c.getContext("2d");
    ctx.textBaseline = "top";
    ctx.font = "14px Arial";
    ctx.fillStyle = "#f60";
    ctx.fillRect(0, 0, 220, 30);
    ctx.fillStyle = "#069";
    ctx.fillText("circus-tent 0123456789", 4, 8);
    canvas = fnv(c.toDataURL());
  } catch (e) { canvas = "error"; }
  let webgl = "";
  try {
    const gl = document.createElement("canvas").getContext("webgl");
    if (gl) {
      const ext = gl.getExtension("WEBGL_debug_renderer_info");
      const parts = [
        gl.getParameter(gl.VERSION), gl.getParameter(gl.VENDOR),
        gl.getParameter(gl.RENDERER),
        ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : "",
        ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : "",
      ];
      webgl = fnv(parts.join("|"));
    }
  } catch (e) { webgl = "error"; }
  let audio = "";
  try {
    const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (Offline) {
      const ac = new Offline(1, 5000, 44100);
      const osc = ac.createOscillator();
      osc.type = "triangle"; osc.frequency.value = 10000;
      const comp = ac.createDynamicsCompressor();
      osc.connect(comp); comp.connect(ac.destination);
      osc.start(0); ac.startRendering();
      const buf = new Float32Array(4500).map((_, i) => i / 4500);
      audio = fnv(buf.map((v) => Math.round(v * 1000) % 256).join(","));
    }
  } catch (e) { audio = "error"; }
  const navigator = {
    userAgent: window.navigator.userAgent || "",
    platform: window.navigator.platform || "",
    languages: (window.navigator.languages || []).join(","),
    hardwareConcurrency: window.navigator.hardwareConcurrency || 0,
    deviceMemory: window.navigator.deviceMemory || 0,
  };
  const screen = {
    width: window.screen.width, height: window.screen.height,
    colorDepth: window.screen.colorDepth,
  };
  let fonts = [];
  try {
    const probes = [
      "Arial", "Verdana", "Helvetica", "Times New Roman", "Courier New",
      "Georgia", "Trebuchet MS", "Comic Sans MS", "Impact", "Palatino",
    ];
    fonts = probes.filter((f) => document.fonts.check(`12px "${f}"`)).slice(0, 40);
  } catch (e) { fonts = []; }
  return JSON.stringify({canvas, webgl, audio, navigator, screen, fonts});
}
"""


@dataclass(frozen=True)
class FingerprintManifest:
    launch_options: dict[str, Any]
    created_at: str
    manifest_hash: str


def _canonical(options: dict[str, Any]) -> str:
    return json.dumps(options, sort_keys=True, separators=(",", ":"))


def build_launch_options(profile_dir: Path, headless: str, humanize: bool = True) -> dict[str, Any]:
    """Full camoufox launch option set. NO random seeds, ever — the resolved
    options become the immutable fingerprint manifest.

    Pinned call shape verified against camoufox 0.5.7 (persistent-context
    options incl. headless="virtual").
    """
    normalized = {"virtual": "virtual", "true": True, "false": False}.get(headless, "virtual")
    return {
        "user_data_dir": str(profile_dir / "user_data"),
        "headless": normalized,
        "humanize": humanize,
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-US",
        "timezone_id": "UTC",
    }


def resolve_manifest(profile_dir: Path, launch_options: dict[str, Any]) -> FingerprintManifest:
    """Create {profile_dir}/fingerprint.json if absent; otherwise load it
    (ignoring the passed options)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)
    path = profile_dir / MANIFEST_FILENAME
    if path.exists():
        data = json.loads(path.read_text())
        return FingerprintManifest(
            launch_options=dict(data["launch_options"]),
            created_at=data["created_at"],
            manifest_hash=data["manifest_hash"],
        )
    manifest_hash = hashlib.sha256(_canonical(launch_options).encode()).hexdigest()
    manifest = FingerprintManifest(
        launch_options=dict(launch_options),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        manifest_hash=manifest_hash,
    )
    payload = {
        "launch_options": manifest.launch_options,
        "created_at": manifest.created_at,
        "manifest_hash": manifest.manifest_hash,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return manifest


def fingerprint_matches(manifest: FingerprintManifest, profile_dir: Path) -> bool:
    path = profile_dir / MANIFEST_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"fingerprint manifest missing: {path}")
    data: dict[str, Any] = json.loads(path.read_text())
    return bool(data.get("manifest_hash") == manifest.manifest_hash)


async def capture_fingerprint(page: Any) -> dict[str, Any]:
    raw = await page.evaluate(FINGERPRINT_JS)
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def compare_fingerprints(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
