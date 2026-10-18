"""Frozen copies: record live SEC answers once, then serve them with the network blocked.

Run from the repository root with EDGAR_USER_AGENT set:
    python -X utf8 tests/test_frozen.py
The recording step makes two live requests to the SEC.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URLS = {
    "submissions": "https://data.sec.gov/submissions/CIK0000315189.json",
    "missing": "https://data.sec.gov/submissions/CIK0000000000.json",
    "unrecorded": "https://data.sec.gov/submissions/CIK0000320193.json",
}


def child(mode):
    """Runs in a fresh process, so the settings read the environment set for it."""
    sys.path.insert(0, HERE)
    from utils import sec_client
    out = {}
    if mode == "frozen":
        def no_network(*a, **k):
            raise AssertionError("the network was used")
        sec_client._client.stream = no_network
    for name in ("submissions", "missing") + (("unrecorded",) if mode == "frozen" else ()):
        try:
            body, content_type = sec_client.fetch(URLS[name])
            out[name] = {"ok": len(body), "type": content_type, "head": body[:40].decode("utf-8", "replace")}
        except Exception as e:
            out[name] = {"error": type(e).__name__, "code": getattr(e, "code", "")}
    print(json.dumps(out))


def run(mode, env):
    result = subprocess.run([sys.executable, "-X", "utf8", __file__, mode], cwd=HERE, capture_output=True,
                            text=True, env={**os.environ, **env})
    if result.returncode:
        print(result.stderr[-2000:])
    return json.loads(result.stdout.strip().splitlines()[-1])


def main():
    problems = []

    def check(label, ok, detail=""):
        print(f"{'OK  ' if ok else 'FAIL'} {label} {detail}")
        if not ok:
            problems.append(label)

    folder = tempfile.mkdtemp(prefix="edgar_frozen_")
    live = run("record", {"EDGAR_RECORD_DIR": folder})
    check("a live answer is returned while recording", "ok" in live["submissions"], str(live["submissions"])[:120])
    check("a live 404 is reported as not found", live["missing"].get("error") == "SecNotFound")
    check("the 200 and the 404 are both saved", len([f for f in os.listdir(folder) if f.endswith(".json")]) == 2)

    frozen = run("frozen", {"EDGAR_FROZEN_DIR": folder})
    check("the frozen copy gives the same answer, network blocked", frozen["submissions"] == live["submissions"])
    check("a frozen 404 is still not found", frozen["missing"].get("error") == "SecNotFound")
    check("an address with no frozen copy is refused, network never tried",
          frozen["unrecorded"].get("code") == "NO_FROZEN_COPY", str(frozen["unrecorded"]))

    body = next(os.path.join(folder, f) for f in os.listdir(folder)
                if f.endswith(".body") and os.path.getsize(os.path.join(folder, f)) > 0)
    with open(body, "ab") as f:
        f.write(b" ")
    tampered = run("frozen", {"EDGAR_FROZEN_DIR": folder})
    check("a changed frozen copy is refused", tampered["submissions"].get("error") == "SecError")

    print(f"problems: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(sys.argv[1])
    else:
        sys.exit(main())
