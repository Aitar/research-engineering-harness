from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REQUIRED_PROVENANCE_FIELDS = {
    "provider",
    "key_id",
    "repository",
    "workflow",
    "run_id",
    "job_id",
    "commit_sha",
    "build_id",
    "build_artifact_sha256",
    "report_sha256",
    "command_sha256",
    "issued_at",
    "nonce",
}


@dataclass(frozen=True)
class VerifiedProvenance:
    payload: dict[str, Any]
    signature: bytes


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def command_digest(command: list[str]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json_bytes(command)).hexdigest()


def load_public_key(public_key_pem: str | bytes) -> Ed25519PublicKey:
    raw = public_key_pem.encode("utf-8") if isinstance(public_key_pem, str) else public_key_pem
    key = serialization.load_pem_public_key(raw)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("CI trust roots must be Ed25519 public keys.")
    return key


def _decode_signature(value: bytes) -> bytes:
    stripped = value.strip()
    try:
        return base64.b64decode(stripped, validate=True)
    except ValueError:
        if len(stripped) == 64:
            return stripped
        raise ValueError("CI provenance signature must be base64-encoded Ed25519 bytes.") from None


def verify_provenance(
    provenance_path: Path,
    signature_path: Path,
    public_key_pem: str | bytes,
) -> VerifiedProvenance:
    try:
        payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid CI provenance JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("CI provenance must be a JSON object.")
    missing = REQUIRED_PROVENANCE_FIELDS - set(payload)
    if missing:
        raise ValueError(f"CI provenance is missing fields: {', '.join(sorted(missing))}")
    signature = _decode_signature(signature_path.read_bytes())
    key = load_public_key(public_key_pem)
    try:
        key.verify(signature, canonical_json_bytes(payload))
    except InvalidSignature as exc:
        raise ValueError("CI provenance signature verification failed.") from exc
    return VerifiedProvenance(payload=payload, signature=signature)
