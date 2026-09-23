"""Turn a cloud job's stdout back into the files a local job would have written.

Everything downstream (log follow, events, ETA, lost time, log-tail diagnosis) then works on
cloud jobs unchanged.
"""

import json
from collections.abc import Callable
from pathlib import Path

from pasar.cloud.control import split


class Pump:
    def __init__(self, provider, handle: str, token: str, job_dir: Path,
                 on_sample: Callable[[list], None], on_exit: Callable[[dict], None]):
        self.provider = provider
        self.handle = handle
        self.token = token
        self.job_dir = job_dir
        self.on_sample = on_sample
        self.on_exit = on_exit
        self.cursor = 0
        self.exit_info: dict | None = None
        self.phase_times: dict[str, float] = {}

    def poll(self) -> None:
        """Idempotent and resumable: `cursor` means everything up to it has been fully
        processed, so it is the only state a caller needs to persist to pick a poll loop back
        up after a restart without re-reading or dropping output.

        Bytes after the last complete line are never decoded or buffered here: `cursor` is
        simply left pointing before them, so the provider re-serves them (plus whatever has
        since arrived) on the next poll. Every provider we support accepts reading from an
        earlier cursor, so this also means a multi-byte UTF-8 character split across two reads
        is always decoded whole, never as two separately-decoded (and separately mangled)
        fragments.
        """
        # `data` runs from the old cursor to whatever the provider has right now; we only
        # advance `self.cursor` up to the last complete line below, so any bytes past that
        # point are simply re-fetched (prefixed to whatever's new) on the next poll.
        data, _ = self.provider.read_output(self.handle, self.cursor)
        if not data:
            return
        # Never str.splitlines() this: the control prefix is \x1e, which splitlines() treats
        # as its own line boundary and would silently split a control line in two. Split on
        # raw bytes, not decoded text: "\n" (0x0A) can never appear inside a multi-byte UTF-8
        # sequence, so this is always a safe place to cut, even mid-character.
        end = data.rfind(b"\n")
        if end == -1:
            return  # no complete line yet; leave cursor where it is
        complete, self.cursor = data[:end + 1], self.cursor + end + 1
        text = complete.decode("utf-8", errors="replace")
        lines = text.split("\n")[:-1]  # `complete` always ends with "\n"
        log_lines, events = [], []
        for line in lines:
            obj = split(line, self.token)
            if obj is None:
                log_lines.append(line)
                continue
            kind = obj.get("t")
            if kind == "event":
                events.append(obj.get("e", {}))
            elif kind == "sample":
                self.on_sample(obj.get("gpus", []))
            elif kind == "phase":
                self.phase_times[obj.get("phase", "?")] = obj.get("ts", 0.0)
            elif kind == "exit":
                self.exit_info = obj
                self.on_exit(obj)
            else:
                # A corrupt or unrecognized control line must not vanish silently, and it must
                # not be mistaken for a clean exit: fall back to treating it as log text.
                log_lines.append(line)
        # Always touch output.log once we've read anything, even if this batch was pure
        # control lines: callers (log follow, tail) expect the file to exist from the job's
        # first output onward, not only from its first line of plain text.
        with (self.job_dir / "output.log").open("a") as f:
            if log_lines:
                f.write("\n".join(log_lines) + "\n")
        if events:
            with (self.job_dir / "events.jsonl").open("a") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")
