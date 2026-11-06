"""End-to-end smoke test of the offline demo, driven through a real terminal.

Usage: python scripts/demo_smoke.py OUTPUT_DIR

OUTPUT_DIR must be an existing folder in your user space. The script runs
`jev --demo --output OUTPUT_DIR onboard --workspace OUTPUT_DIR/project`, answers
the interview like a person would (/review, /approve, yes, /done), then runs
run -> verify -> apply -> complete with the same jev executable and checks that
every step exits 0. Standard library only, so it works in a clean virtualenv.
"""

import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path


def jev_executable():
    found = Path(sys.executable).with_name("jev")
    return str(found) if found.exists() else shutil.which("jev")


class Terminal:
    def __init__(self, argv):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execv(argv[0], argv)
        self.buffer = ""

    def expect(self, pattern, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if re.search(pattern, self.buffer):
                found = self.buffer
                self.buffer = ""
                return found
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.fd, 4096).decode("utf-8", "replace")
                except OSError:
                    break
                sys.stdout.write(chunk)
                sys.stdout.flush()
                self.buffer += chunk
        raise SystemExit("demo smoke: timed out waiting for " + repr(pattern))

    def say(self, text):
        os.write(self.fd, (text + "\n").encode())

    def wait(self):
        _, status = os.waitpid(self.pid, 0)
        return os.waitstatus_to_exitcode(status)


def step(argv, *, expect_json=False):
    print("\n$ " + " ".join(argv), flush=True)
    result = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    sys.stdout.write(result.stdout + result.stderr)
    if result.returncode != 0:
        raise SystemExit("demo smoke: step failed with exit " + str(result.returncode))
    return json.loads(result.stdout) if expect_json else result.stdout


def main():
    output = Path(sys.argv[1]).resolve()
    project = output / "project"
    project.mkdir(parents=True, exist_ok=True)
    jev = jev_executable()
    base = [jev, "--demo", "--output", str(output)]
    term = Terminal([*base, "onboard", "--workspace", str(project)])
    term.expect(r"You: ")
    term.say("Create hello.txt containing hello world.")
    term.expect(r"You: ")
    term.say("/review")
    term.expect(r"You: ")
    term.say("/approve")
    term.expect(r"yes or no")
    term.say("yes")
    term.expect(r"approval is recorded[\s\S]*You: ")
    term.say("/done")
    term.expect(r"Conversation saved")
    if term.wait() != 0:
        raise SystemExit("demo smoke: onboarding did not exit 0")
    records = step([*base, "inspect", "--json"], expect_json=True)
    proposal = records[-1][0]
    out = step(
        [*base, "run", "--workspace", str(project), "--no-provider", "--"]
        + ["sh", "-c", "echo hello world > hello.txt"]
    )
    run_id = re.search(r"Session: (\S+)", out).group(1)
    step([*base, "verify", run_id])
    step([*base, "apply", run_id])
    step([*base, "complete", proposal])
    if (project / "hello.txt").read_text().strip() != "hello world":
        raise SystemExit("demo smoke: hello.txt was not applied")
    print("\ndemo smoke: passed (onboard, review, approve, run, verify, apply, complete)")


if __name__ == "__main__":
    main()
