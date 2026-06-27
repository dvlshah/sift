"""RFC-3161 external timestamp anchor for a published Merkle root.

Closes the "what stops you back-dating the root?" gap. An independent Time-Stamp
Authority (TSA) — DigiCert, Sectigo, etc., recognized under eIDAS — cryptographically
attests that a digest (the snapshot's ``merkle_root``) was presented at a given time,
signed by a party that neither sift nor the reader controls. The resulting token is a
standard RFC-3161 ``.tsr`` verifiable with ``openssl ts -verify`` — no sift install.

Optional and off by default (set ``[publish].timestamp_tsa_url``). Mirrors the GPG
signing pattern in publish.py: subprocess to the standard tool, non-fatal on failure,
so a TSA outage degrades the publish to "unwitnessed" rather than failing it.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

TSQ_CONTENT_TYPE = "application/timestamp-query"


def _openssl() -> Optional[str]:
    return shutil.which("openssl")


def openssl_ts_available() -> bool:
    """True iff an ``openssl`` with the RFC-3161 ``ts`` subcommand is on PATH.
    macOS's default LibreSSL lacks ``ts``; an OpenSSL build (brew/Linux) has it."""
    ossl = _openssl()
    if ossl is None:
        return False
    try:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "probe.tsq"
            r = subprocess.run(
                [ossl, "ts", "-query", "-digest", "00" * 32, "-sha256", "-out", str(out)],
                capture_output=True, timeout=10,
            )
            return r.returncode == 0 and out.exists()
    except (subprocess.SubprocessError, OSError):
        return False


def request_timestamp(
    digest_hex: str, tsa_url: str, *, hash_alg: str = "sha256", timeout: float = 30.0,
) -> bytes:
    """Request an RFC-3161 timestamp token (``.tsr`` bytes) over ``digest_hex`` from
    ``tsa_url``. Raises on failure (caller decides whether that's fatal)."""
    ossl = _openssl()
    if ossl is None:
        raise RuntimeError("openssl not found on PATH; cannot build a timestamp request.")
    with tempfile.TemporaryDirectory() as d:
        tsq = Path(d) / "req.tsq"
        # -cert: ask the TSA to embed its signing cert chain in the token, so the
        # token is self-contained (a verifier needs only the public root CA).
        subprocess.run(
            [ossl, "ts", "-query", "-digest", digest_hex, f"-{hash_alg}", "-cert",
             "-out", str(tsq)],
            check=True, capture_output=True, timeout=15,
        )
        req = urllib.request.Request(
            tsa_url, data=tsq.read_bytes(),
            headers={"Content-Type": TSQ_CONTENT_TYPE, "Accept": "application/timestamp-reply"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-set URL)
            tst = resp.read()
        if not tst:
            raise RuntimeError(f"TSA {tsa_url} returned an empty response.")
        # Sanity: it must parse as a timestamp reply, or it isn't a token.
        tstf = Path(d) / "resp.tsr"
        tstf.write_bytes(tst)
        chk = subprocess.run(
            [ossl, "ts", "-reply", "-in", str(tstf), "-text"],
            capture_output=True, timeout=15,
        )
        if chk.returncode != 0:
            raise RuntimeError(
                f"TSA {tsa_url} response is not a valid RFC-3161 token: "
                f"{chk.stderr.decode('utf-8', 'replace')[:200]}"
            )
        return tst


def parse_timestamp(tst_bytes: bytes) -> dict:
    """Human-readable fields from a token: time, hash_algorithm, tsa, serial."""
    ossl = _openssl()
    if ossl is None:
        return {}
    with tempfile.TemporaryDirectory() as d:
        tstf = Path(d) / "token.tsr"
        tstf.write_bytes(tst_bytes)
        r = subprocess.run([ossl, "ts", "-reply", "-in", str(tstf), "-text"],
                           capture_output=True, text=True, timeout=15)
    out: dict = {}
    for line in (r.stdout or "").splitlines():
        s = line.strip()
        if s.startswith("Time stamp:"):
            out["time"] = s.split(":", 1)[1].strip()
        elif s.startswith("Hash Algorithm:"):
            out["hash_algorithm"] = s.split(":", 1)[1].strip()
        elif s.startswith("TSA:"):
            out["tsa"] = s.split(":", 1)[1].strip()
        elif s.startswith("Serial number:"):
            out["serial"] = s.split(":", 1)[1].strip()
    return out


def verify_timestamp(
    tst_bytes: bytes, digest_hex: str, *, ca_file: Optional[Path] = None,
) -> dict:
    """Verify a token: the TSA signature chains to a trusted root AND its message
    imprint equals ``digest_hex``. Returns {ok, time, tsa, error}. ``ca_file`` is the
    trust anchor (a CA bundle, e.g. certifi's); without it openssl uses its default."""
    ossl = _openssl()
    if ossl is None:
        return {"ok": False, "error": "openssl not found on PATH"}
    info = parse_timestamp(tst_bytes)
    with tempfile.TemporaryDirectory() as d:
        tstf = Path(d) / "token.tsr"
        tstf.write_bytes(tst_bytes)
        cmd = [ossl, "ts", "-verify", "-digest", digest_hex, "-in", str(tstf)]
        if ca_file is not None:
            cmd += ["-CAfile", str(ca_file)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    ok = r.returncode == 0 and "Verification: OK" in (r.stdout + r.stderr)
    return {
        "ok": ok,
        "time": info.get("time"),
        "tsa": info.get("tsa"),
        "error": None if ok else (r.stderr.strip() or r.stdout.strip() or "verification failed"),
    }


def default_ca_file() -> Optional[Path]:
    """A CA bundle to anchor TSA trust, if available (certifi ships DigiCert/Sectigo
    roots). Auditors can instead use their OS trust store."""
    try:
        import certifi
        return Path(certifi.where())
    except Exception:
        return None
