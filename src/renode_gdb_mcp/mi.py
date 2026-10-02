"""Asynchronous GDB/MI driver.

Runs one ``gdb`` process in MI mode with ``mi-async on`` so that resuming the
target (``-exec-continue``) returns immediately.  This matters for Renode: while
the simulated CPU runs, other clients (the Renode MCP, Java tests talking to a
UART socket, ...) keep interacting with the emulation, and the MCP must stay
responsive so the agent can interrupt, inspect, or wait for a breakpoint.
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pygdbmi.gdbmiparser import parse_response

log = logging.getLogger(__name__)

# Result classes that terminate a command.
_RESULT_CLASSES = {"done", "running", "connected", "error", "exit"}


class GdbError(Exception):
    """GDB answered a command with ``^error`` or could not be reached."""


@dataclass
class MiResult:
    cls: str  # done / running / connected / error / exit
    payload: dict[str, Any]
    console: list[str] = field(default_factory=list)
    target: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Console + target output, as a human would see it in the gdb CLI."""
        return "".join(self.console + self.target).rstrip("\n")


@dataclass
class Event:
    id: int
    ts: float
    kind: str  # stopped / running / console / target / log / notify / gdb-exit
    text: str
    data: Any = None


def mi_quote(s: str) -> str:
    """Quote an argument as an MI c-string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class GdbMi:
    """One GDB process plus the state of the target it is attached to."""

    def __init__(self, gdb_path: str, *, event_capacity: int = 2000) -> None:
        self.gdb_path = gdb_path
        self.proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._tokens = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._collector: MiResult | None = None
        self._cmd_lock = asyncio.Lock()
        self._state_cond = asyncio.Condition()
        self._event_ids = itertools.count(1)
        self.events: collections.deque[Event] = collections.deque(maxlen=event_capacity)
        self._partial: dict[str, str] = {}  # unterminated async output per stream

        # Target state as seen by gdb.
        self.connected = False
        self.target_desc = ""
        self.state = "idle"  # idle / stopped / running / exited / gdb-exited
        self.last_stop: dict[str, Any] | None = None
        self.stop_seq = 0
        self.running_since: float | None = None

    # ------------------------------------------------------------------ process

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.gdb_path,
            "--interpreter=mi3",
            "--quiet",
            "--nx",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=16 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._reader())
        for setting in (
            "mi-async on",
            "pagination off",
            "confirm off",
            "width 0",
            "height 0",
            "print pretty on",
            "dprintf-style gdb",
            "breakpoint pending on",
        ):
            await self.command(f"-gdb-set {setting}")

    async def close(self) -> None:
        if self.alive:
            try:
                await self.command("-gdb-exit", timeout=3)
            except Exception:  # noqa: BLE001 - best effort shutdown
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._reader_task:
            self._reader_task.cancel()
        self.connected = False

    # ------------------------------------------------------------------ commands

    async def command(self, cmd: str, timeout: float = 15.0) -> MiResult:
        """Send one MI command and return its result record.

        Raises GdbError on ``^error`` or timeout.
        """
        if not self.alive:
            raise GdbError("gdb is not running (connect first)")
        async with self._cmd_lock:
            token = next(self._tokens)
            fut = asyncio.get_running_loop().create_future()
            self._pending[token] = fut
            self._collector = MiResult(cls="", payload={})
            log.debug("-> %d%s", token, cmd)
            self.proc.stdin.write(f"{token}{cmd}\n".encode())
            await self.proc.stdin.drain()
            try:
                cls, payload = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                raise GdbError(f"gdb did not answer within {timeout:g}s: {cmd}") from None
            finally:
                self._pending.pop(token, None)
                result, self._collector = self._collector, None
        result.cls, result.payload = cls, payload or {}
        if cls == "error":
            msg = result.payload.get("msg", "unknown error")
            extra = result.text
            raise GdbError(msg + (f"\n{extra}" if extra else ""))
        if cls == "running":
            await self._set_running()
        return result

    async def console(self, cli: str, timeout: float = 15.0) -> MiResult:
        """Run a gdb CLI command and capture its console output."""
        return await self.command(f"-interpreter-exec console {mi_quote(cli)}", timeout)

    async def interrupt(self) -> None:
        if self.alive:
            await self.command("-exec-interrupt")

    # ------------------------------------------------------------------ state

    async def _set_running(self) -> None:
        async with self._state_cond:
            if self.state != "running":
                self.state = "running"
                self.running_since = time.monotonic()
            self._state_cond.notify_all()

    async def wait_for_stop(self, timeout: float, after_seq: int | None = None) -> bool:
        """Wait until the target is stopped (and, if given, a stop newer than
        ``after_seq`` was seen).  Returns False on timeout."""

        def done() -> bool:
            if self.state in ("exited", "gdb-exited", "idle"):
                return True
            if after_seq is not None and self.stop_seq <= after_seq:
                return False
            return self.state == "stopped"

        async with self._state_cond:
            try:
                await asyncio.wait_for(self._state_cond.wait_for(done), timeout)
                return True
            except asyncio.TimeoutError:
                return False

    def add_event(self, kind: str, text: str, data: Any = None) -> None:
        self.events.append(Event(next(self._event_ids), time.time(), kind, text, data))

    # ------------------------------------------------------------------ reader

    async def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line or line == "(gdb)":
                    continue
                log.debug("<- %s", line)
                try:
                    rec = parse_response(line)
                except Exception:  # noqa: BLE001 - never kill the reader
                    rec = {"type": "output", "message": None, "payload": line}
                await self._dispatch(rec)
        finally:
            async with self._state_cond:
                self.state = "gdb-exited"
                self.connected = False
                self._state_cond.notify_all()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(GdbError("gdb process exited"))
            self.add_event("gdb-exit", "gdb process exited")

    async def _dispatch(self, rec: dict[str, Any]) -> None:
        rtype, msg, payload = rec.get("type"), rec.get("message"), rec.get("payload")

        if rtype == "result" and msg in _RESULT_CLASSES:
            fut = self._pending.get(rec.get("token"))
            if fut and not fut.done():
                fut.set_result((msg, payload))
            return

        if rtype in ("console", "target", "log", "output"):
            text = payload if isinstance(payload, str) else str(payload)
            if self._collector is not None:
                bucket = {"console": self._collector.console, "target": self._collector.target}
                bucket.get(rtype, self._collector.log).append(text)
            else:
                # Output produced while no command is pending: dprintf hits,
                # semihosting / monitor output, async warnings...  gdb emits it
                # in fragments; only log complete lines.
                buf = self._partial.get(rtype, "") + text
                *lines, rest = buf.split("\n")
                self._partial[rtype] = rest
                for ln in lines:
                    if ln.strip():
                        self.add_event(rtype, ln)
            return

        if rtype == "notify":
            if msg == "stopped":
                payload = payload or {}
                async with self._state_cond:
                    reason = payload.get("reason", "")
                    self.state = "exited" if reason.startswith("exited") else "stopped"
                    self.last_stop = payload
                    self.stop_seq += 1
                    self.running_since = None
                    self._state_cond.notify_all()
                self.add_event("stopped", describe_stop(payload), payload)
            elif msg == "running":
                await self._set_running()
                self.add_event("running", "target running")
            elif msg == "thread-group-exited":
                self.add_event("notify", "target exited / disconnected", payload)


def describe_frame(frame: dict[str, Any] | None) -> str:
    if not frame:
        return "<no frame>"
    func = frame.get("func", "??")
    where = f"{func} ()"
    if frame.get("file"):
        where += f" at {frame.get('file')}:{frame.get('line', '?')}"
    elif frame.get("from"):
        where += f" from {frame['from']}"
    if frame.get("addr"):
        where += f" [pc={frame['addr']}]"
    return where


def describe_stop(stop: dict[str, Any] | None) -> str:
    if not stop:
        return "no stop recorded"
    reason = stop.get("reason", "halted")
    bits = [reason]
    if stop.get("bkptno"):
        bits.append(f"breakpoint #{stop['bkptno']}")
    if stop.get("signal-name"):
        bits.append(f"signal {stop['signal-name']}")
    wpt = stop.get("wpt") or stop.get("hw-awpt") or stop.get("hw-rwpt")
    if isinstance(wpt, dict):
        bits.append(f"watchpoint #{wpt.get('number')} on {wpt.get('exp')}")
    val = stop.get("value")
    if isinstance(val, dict):
        if "old" in val:
            bits.append(f"old={val['old']}")
        if "new" in val:
            bits.append(f"new={val['new']}")
    if stop.get("thread-id"):
        bits.append(f"thread {stop['thread-id']}")
    return f"Stopped ({', '.join(bits)}) in {describe_frame(stop.get('frame'))}"
