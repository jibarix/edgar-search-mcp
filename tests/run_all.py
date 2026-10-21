"""Run every test script, offline, and report the total.

    python -X utf8 tests/run_all.py
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = ["test_cases.py", "test_tools_offline.py", "test_frozen.py", "test_contract.py", "test_mcp_stdio.py"]


def main():
    failed, total_ok = [], 0
    for script in SCRIPTS:
        result = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, script)], cwd=os.path.dirname(HERE),
                                capture_output=True, text=True)
        out = result.stdout
        ok = len(re.findall(r"(?m)^OK ", out))
        fails = re.findall(r"(?m)^FAIL .*$", out)
        summary = re.findall(r"(?m)^.*problems: \d+.*$", out)
        total_ok += ok
        print(f"{script:24} {ok:4} OK  {len(fails):3} FAIL  {summary[-1] if summary else 'no summary'}"
              f"  exit {result.returncode}")
        for line in fails:
            print("   ", line[:160])
        if result.returncode or fails or not summary:
            failed.append(script)
            if not summary:
                print(result.stderr[-1500:])
    print(f"total: {total_ok} OK, {len(failed)} script(s) failing {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
