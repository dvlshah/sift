#!/usr/bin/env python3
"""Standalone verifier for a sift proof envelope. Stdlib only; no sift install.

Verifies that a page's content_hash is committed by a published snapshot's
Merkle root using ONLY the envelope file. It reimplements sift's two tree rules
(the leaf hash and the parent concat/order from sift/integrity.py) so a third
party can audit a proof WITHOUT installing or trusting sift — copy this one file
anywhere Python runs.

    python -m sift.verify_proof PROOF.json [--expect-root HEX]

Exit: 0 valid, 1 invalid, 2 bad input. Pinned to integrity_version "v1".

Note on the hashing convention (so a re-implementer gets it right): nodes are
combined as the SHA-256 of the *concatenated lowercase-hex strings* (128 hex
chars → utf-8 bytes), NOT raw 32-byte digests and NOT double-SHA-256. The leaf
is sha256(utf8(url + ':' + content_hash_hex)) where content_hash_hex is the
content_hash with any leading 'sha256:' removed.
"""
import hashlib
import json
import sys

SUPPORTED_INTEGRITY_VERSION = "v1"      # must equal sift integrity.INTEGRITY_VERSION
SUPPORTED_SCHEME = "sorted-leaves-bitcoin-style-sha256"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def leaf_hash(url: str, content_hash_hex: str) -> str:
    # MIRRORS sift/integrity.py leaf_hash — bare hex, single ':' join.
    return _sha(f"{url}:{content_hash_hex}".encode("utf-8"))


def fold(leaf: str, proof: list) -> str:
    # MIRRORS sift/integrity.py parent op — hex-string concat; position of sibling.
    node = leaf
    for step in proof:
        sib, pos = step["sibling"], step["position"]
        if pos == "left":
            node = _sha((sib + node).encode("utf-8"))
        elif pos == "right":
            node = _sha((node + sib).encode("utf-8"))
        else:
            raise ValueError(f"bad position {pos!r}")
    return node


def verify(env: dict, expect_root=None):
    """Return (ok: bool, notes: list[str]). Uses only the envelope."""
    if env.get("integrity_version") != SUPPORTED_INTEGRITY_VERSION:
        return False, [
            f"unsupported integrity_version {env.get('integrity_version')!r}; "
            f"this checker implements {SUPPORTED_INTEGRITY_VERSION!r}"
        ]
    if env.get("scheme") != SUPPORTED_SCHEME:
        return False, [f"unexpected scheme {env.get('scheme')!r}"]
    ch_hex = str(env["content_hash"]).removeprefix("sha256:")
    leaf = leaf_hash(env["url"], ch_hex)
    leaf_ok = (leaf == env["leaf"])
    root = fold(env["leaf"], env["proof"])
    root_ok = (root == env["merkle_root"])
    notes = [
        f"leaf {'OK' if leaf_ok else 'MISMATCH'}",
        f"root {'OK' if root_ok else 'MISMATCH'} "
        f"(recomputed {root[:16]}… vs envelope {str(env['merkle_root'])[:16]}…)",
    ]
    bound_ok = True
    if expect_root is not None:
        bound_ok = (env["merkle_root"] == expect_root)
        notes.append(f"expect-root {'OK' if bound_ok else 'MISMATCH'}")
    return (leaf_ok and root_ok and bound_ok), notes


def main(argv) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    expect = argv[argv.index("--expect-root") + 1] if "--expect-root" in argv else None
    try:
        with open(argv[1], encoding="utf-8") as fh:
            env = json.loads(fh.read())
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "reason": f"unreadable proof: {e}"}))
        return 2
    try:
        ok, notes = verify(env, expect)
    except (KeyError, ValueError, TypeError) as e:
        print(json.dumps({"ok": False, "reason": f"malformed envelope: {e}"}))
        return 2
    print(json.dumps({
        "ok": ok, "url": env.get("url"), "run_id": env.get("run_id"),
        "merkle_root": env.get("merkle_root"), "checks": notes,
    }, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
