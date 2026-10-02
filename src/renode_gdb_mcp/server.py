"""MCP server exposing a GDB client tailored for Renode's GDB stub.

Renode side::

    (monitor) machine StartGdbServer 3333 true

``true`` = start the emulation automatically when GDB connects / continues.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from .mi import GdbError, GdbMi, describe_frame, describe_stop, mi_quote

INSTRUCTIONS = """\
GDB client for firmware running in the Renode emulator (Renode's GDB stub, started in
the Renode Monitor with `machine StartGdbServer <port> true`).

This server is meant to be used ALONGSIDE:
- a Renode MCP (drives the Renode Monitor: create machines, load ELF, start/pause/reset,
  read peripherals), and
- an IDE MCP (e.g. IntelliJ IDEA) that runs Java tests talking to the simulated firmware.

Typical workflow:
1. Renode MCP: load the platform + ELF, then `machine StartGdbServer 3333 true`.
2. gdb_connect(port=3333, elf=<same ELF>). The CPU is halted while GDB is attached and
   stopped.
3. Set breakpoints / gdb_dprintf log points, then gdb_continue(wait=false). It returns
   immediately so you can start the Java tests via the IDE MCP while firmware runs.
4. gdb_wait_for_stop(timeout) to catch a breakpoint hit, then inspect with gdb_backtrace,
   gdb_evaluate, gdb_registers, gdb_read_memory. gdb_events shows dprintf output and
   stops that happened in the meantime.

Important Renode interactions:
- While GDB holds the CPU halted, virtual time of that machine does not advance: firmware
  timers, UART output, and replies to your Java tests are frozen. Tests with wall-clock
  timeouts may fail. Prefer gdb_dprintf (non-halting log points) for observing running
  tests, and keep halts short.
- If the emulation is paused in Renode (Renode MCP `pause`), GDB still thinks the target
  is "running" but nothing progresses. Make sure the emulation is started.
- After a reset or memory/register write done through the Renode MCP, call gdb_resync so
  GDB drops its stale caches.
- Reading MMIO (peripheral) addresses through gdb_read_memory performs real bus reads and
  can have side effects (e.g. clearing status flags). Prefer the Renode MCP for
  peripheral inspection.
- Multi-core machines: each CPU appears as a GDB thread (gdb_threads / gdb_select_thread).
- Several machines: give each its own GDB port and use a distinct `session` name per
  connection.
"""

mcp = FastMCP("renode-gdb", instructions=INSTRUCTIONS, log_level=os.environ.get("RENODE_GDB_MCP_LOG", "WARNING").upper())

GDB_CANDIDATES = (
    "gdb-multiarch",
    "arm-none-eabi-gdb",
    "arm-zephyr-eabi-gdb",
    "riscv64-unknown-elf-gdb",
    "riscv32-unknown-elf-gdb",
    "riscv64-zephyr-elf-gdb",
    "gdb",
)

SessionArg = Annotated[
    str, Field(description="Connection name; use distinct names to debug several Renode machines/ports.")
]


@dataclass
class Session:
    name: str
    gdb: GdbMi
    host: str
    port: int
    elf: str | None
    gdb_path: str
    little_endian: bool = True
    connected_at: float = 0.0


SESSIONS: dict[str, Session] = {}


# ---------------------------------------------------------------------- helpers


def _find_gdb(explicit: str | None) -> str:
    candidates = [explicit] if explicit else []
    if os.environ.get("RENODE_GDB_MCP_GDB"):
        candidates.append(os.environ["RENODE_GDB_MCP_GDB"])
    candidates += GDB_CANDIDATES if not explicit else []
    for c in candidates:
        path = shutil.which(c) if c else None
        if path:
            return path
    raise ToolError(
        f"No usable gdb found (tried: {', '.join(c for c in candidates if c)}). "
        "Install gdb-multiarch or a cross gdb, or pass gdb_path / set RENODE_GDB_MCP_GDB."
    )


def _session(name: str) -> Session:
    s = SESSIONS.get(name)
    if s is None or not s.gdb.alive:
        known = ", ".join(SESSIONS) or "none"
        raise ToolError(f"No active GDB session '{name}' (active: {known}). Call gdb_connect first.")
    return s


async def _run(coro):
    try:
        return await coro
    except GdbError as e:
        raise ToolError(str(e)) from None


def _running_hint(s: Session) -> str:
    since = s.gdb.running_since
    dur = f" for {time.monotonic() - since:.1f}s" if since else ""
    return (
        f"Target is running{dur}. Call gdb_interrupt (or gdb_wait_for_stop) first. "
        "To read memory/peripherals without halting, use the Renode MCP "
        "(e.g. `sysbus ReadDoubleWord 0x...`)."
    )


def _require_stopped(s: Session) -> None:
    if s.gdb.state == "running":
        raise ToolError(_running_hint(s))
    if s.gdb.state != "stopped":
        raise ToolError(f"Target is not stopped (state: {s.gdb.state}).")


@contextlib.asynccontextmanager
async def _halted(s: Session, auto_halt: bool, timeout: float = 5.0):
    """Ensure the target is halted for the body; resume afterwards if we halted it."""
    if s.gdb.state != "running":
        _require_stopped(s)
        yield False
        return
    if not auto_halt:
        raise ToolError(_running_hint(s))
    await _run(s.gdb.interrupt())
    if not await s.gdb.wait_for_stop(timeout):
        raise ToolError(
            "Could not halt the target to apply the change (no stop within "
            f"{timeout:g}s). Is the emulation paused in Renode?"
        )
    try:
        yield True
    finally:
        if s.gdb.state == "stopped":
            await _run(s.gdb.command("-exec-continue"))


async def _where(s: Session) -> str:
    """Current frame + source line, e.g. after a stop."""
    try:
        r = await s.gdb.console("frame", timeout=5)
        if r.text:
            return r.text
    except GdbError:
        pass
    return describe_frame((s.gdb.last_stop or {}).get("frame"))


async def _stop_report(s: Session) -> str:
    g = s.gdb
    if g.state == "exited":
        return f"Target exited: {describe_stop(g.last_stop)}"
    if g.state != "stopped":
        return f"State: {g.state}"
    return f"{describe_stop(g.last_stop)}\n{await _where(s)}"


async def _resume_and_maybe_wait(s: Session, mi_cmd: str, wait: bool, timeout: float) -> str:
    seq = s.gdb.stop_seq
    await _run(s.gdb.command(mi_cmd))
    if not wait:
        return (
            "Target resumed (running). Use gdb_wait_for_stop to wait for a breakpoint, "
            "gdb_interrupt to halt, gdb_events for dprintf output."
        )
    if await s.gdb.wait_for_stop(timeout, after_seq=seq):
        return await _stop_report(s)
    return (
        f"Still running after {timeout:g}s (no breakpoint hit yet). The target keeps running; "
        "call gdb_wait_for_stop again or gdb_interrupt. If nothing should be blocking, check "
        "that the emulation is started in Renode (a paused emulation looks like a running "
        "target to GDB)."
    )


def _fmt_bkpt(b: dict[str, Any]) -> str:
    num, typ = b.get("number", "?"), b.get("type", "breakpoint")
    en = "enabled" if b.get("enabled") == "y" else "disabled"
    if b.get("what") and "watchpoint" in typ:
        where = b["what"]
    elif b.get("func") or b.get("file"):
        where = f"{b.get('func', '??')} at {b.get('file', '?')}:{b.get('line', '?')}"
    else:
        where = b.get("original-location") or b.get("at") or b.get("pending") or "?"
    parts = [f"#{num} {typ} ({en}, {b.get('disp', 'keep')})", where]
    if b.get("addr") and b["addr"] not in ("<MULTIPLE>", "<PENDING>"):
        parts.append(f"addr={b['addr']}")
    if b.get("pending"):
        parts.append("PENDING")
    parts.append(f"hits={b.get('times', '0')}")
    if b.get("cond"):
        parts.append(f"if {b['cond']}")
    if b.get("ignore"):
        parts.append(f"ignore next {b['ignore']}")
    if b.get("thread"):
        parts.append(f"thread {b['thread']}")
    if b.get("script"):
        parts.append(f"script={b['script']}")
    return "  ".join(parts)


def _parse_int(v: str | int) -> int:
    return v if isinstance(v, int) else int(str(v), 0)


def _hexdump(base: int, data: bytes, fmt: str, little: bool) -> str:
    out = []
    if fmt in ("hex", "bytes"):
        for off in range(0, len(data), 16):
            chunk = data[off : off + 16]
            hx = " ".join(f"{b:02x}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            out.append(f"0x{base + off:08x}: {hx:<47}  |{asc}|" if fmt == "hex" else f"0x{base + off:08x}: {hx}")
        return "\n".join(out)
    size = {"words8": 1, "words16": 2, "words32": 4, "words64": 8}[fmt]
    order = "little" if little else "big"
    per_line = max(1, 16 // size)
    words = [data[i : i + size] for i in range(0, len(data) - len(data) % size, size)]
    for i in range(0, len(words), per_line):
        vals = " ".join(f"0x{int.from_bytes(w, order):0{size * 2}x}" for w in words[i : i + per_line])
        out.append(f"0x{base + i * size:08x}: {vals}")
    if len(data) % size:
        out.append(f"(+{len(data) % size} trailing bytes not shown)")
    return "\n".join(out)


# ---------------------------------------------------------------------- connection


@mcp.tool()
async def gdb_connect(
    port: Annotated[int, Field(description="Port given to `machine StartGdbServer <port>` in Renode.")] = int(
        os.environ.get("RENODE_GDB_PORT", "3333")
    ),
    host: str = os.environ.get("RENODE_GDB_HOST", "localhost"),
    elf: Annotated[
        str | None, Field(description="ELF with debug symbols (same file loaded with `sysbus LoadELF` in Renode).")
    ] = None,
    gdb_path: Annotated[
        str | None,
        Field(description="gdb executable. Default: $RENODE_GDB_MCP_GDB, then gdb-multiarch, arm-none-eabi-gdb, ..."),
    ] = None,
    architecture: Annotated[
        str | None, Field(description="Force `set architecture` (e.g. 'arm', 'riscv:rv32'); normally taken from the ELF.")
    ] = None,
    init_commands: Annotated[
        list[str] | None, Field(description="Extra gdb CLI commands run before connecting (e.g. 'set substitute-path a b').")
    ] = None,
    connect_timeout: Annotated[
        float, Field(description="Keep retrying 'connection refused' this long (Renode may still be starting the server).")
    ] = 10.0,
    session: SessionArg = "default",
) -> str:
    """Start gdb, load symbols and attach to Renode's GDB server. Replaces an existing session of the same name."""
    if session in SESSIONS:
        await SESSIONS.pop(session).gdb.close()

    path = _find_gdb(gdb_path)
    if elf and not os.path.isfile(elf):
        raise ToolError(f"ELF not found: {elf}")
    g = GdbMi(path)
    try:
        await g.start()
        if elf:
            await g.command(f"-file-exec-and-symbols {mi_quote(os.path.abspath(elf))}", timeout=60)
        if architecture:
            await g.console(f"set architecture {architecture}")
        for c in init_commands or []:
            await g.console(c)

        deadline = time.monotonic() + connect_timeout
        while True:
            try:
                await g.command(f"-target-select remote {host}:{port}", timeout=20)
                break
            except GdbError as e:
                transient = any(t in str(e).lower() for t in ("refused", "timed out", "connection reset"))
                if not transient or time.monotonic() > deadline:
                    raise GdbError(
                        f"Could not connect to {host}:{port}: {e}\n"
                        "Is the GDB server started in Renode (`machine StartGdbServer "
                        f"{port} true`) and the port free (only one GDB client at a time)?"
                    ) from None
                await asyncio.sleep(0.5)
    except GdbError as e:
        await g.close()
        raise ToolError(str(e)) from None

    s = Session(session, g, host, port, elf, path, connected_at=time.time())
    g.connected = True
    with contextlib.suppress(GdbError):
        s.little_endian = "big" not in (await g.console("show endian")).text
    SESSIONS[session] = s
    # Renode halts the CPU on attach; give gdb a moment to report the stop.
    await g.wait_for_stop(2.0)
    if g.state == "running":
        hint = "Target reports running."
    else:
        g.state = "stopped" if g.state == "idle" else g.state
        hint = await _stop_report(s)
    syms = f", symbols: {elf}" if elf else " (no ELF: no symbols, only raw addresses)"
    return f"Connected session '{session}' to {host}:{port} using {path}{syms}.\n{hint}"


@mcp.tool()
async def gdb_disconnect(session: SessionArg = "default") -> str:
    """Detach from Renode (removes breakpoints; the CPU continues per Renode's emulation state) and stop gdb."""
    s = SESSIONS.pop(session, None)
    if s is None:
        return f"No session '{session}'."
    if s.gdb.alive:
        with contextlib.suppress(GdbError):
            if s.gdb.state == "running":
                await s.gdb.interrupt()
                await s.gdb.wait_for_stop(3)
            await s.gdb.command("-target-detach", timeout=5)
        await s.gdb.close()
    return f"Session '{session}' detached and closed."


@mcp.tool()
async def gdb_status(session: SessionArg = "default") -> str:
    """Connection and execution state (running/stopped, last stop, location). Also lists all sessions."""
    lines = []
    others = [n for n in SESSIONS if n != session]
    s = SESSIONS.get(session)
    if s is None:
        lines.append(f"Session '{session}': not connected.")
    else:
        g = s.gdb
        lines.append(f"Session '{session}': {s.host}:{s.port}, gdb={s.gdb_path}, elf={s.elf or '-'}")
        lines.append(f"gdb process: {'alive' if g.alive else 'dead'}; target state: {g.state}")
        if g.state == "running" and g.running_since:
            lines.append(f"Running for {time.monotonic() - g.running_since:.1f}s (wall clock).")
        elif g.state in ("stopped", "exited"):
            lines.append(await _stop_report(s))
        last_event = g.events[-1].id if g.events else 0
        lines.append(f"Stops seen: {g.stop_seq}; latest event id: {last_event}")
    if others:
        lines.append("Other sessions: " + ", ".join(f"{n} ({SESSIONS[n].gdb.state})" for n in others))
    return "\n".join(lines)


# ---------------------------------------------------------------------- execution control


@mcp.tool()
async def gdb_continue(
    wait: Annotated[
        bool,
        Field(description="false (default): return immediately, e.g. to then run Java tests. true: block until a stop or timeout."),
    ] = False,
    timeout: float = 30.0,
    session: SessionArg = "default",
) -> str:
    """Resume the CPU. Non-blocking by default; pair with gdb_wait_for_stop."""
    s = _session(session)
    if s.gdb.state == "running":
        return "Already running."
    _require_stopped(s)
    return await _resume_and_maybe_wait(s, "-exec-continue", wait, timeout)


@mcp.tool()
async def gdb_wait_for_stop(
    timeout: Annotated[float, Field(description="Seconds (wall clock) to wait.")] = 30.0,
    session: SessionArg = "default",
) -> str:
    """Wait until the target stops (breakpoint, watchpoint, interrupt, step end). Returns immediately if already stopped."""
    s = _session(session)
    if await s.gdb.wait_for_stop(timeout):
        return await _stop_report(s)
    return (
        f"No stop within {timeout:g}s; target still running. Call again to keep waiting, or "
        "gdb_interrupt. (If the emulation is paused in Renode, nothing will progress.)"
    )


@mcp.tool()
async def gdb_interrupt(timeout: float = 5.0, session: SessionArg = "default") -> str:
    """Halt the running CPU (like Ctrl-C in gdb) and report where it stopped."""
    s = _session(session)
    if s.gdb.state != "running":
        return f"Not running.\n{await _stop_report(s)}"
    seq = s.gdb.stop_seq
    await _run(s.gdb.interrupt())
    if await s.gdb.wait_for_stop(timeout, after_seq=seq):
        return await _stop_report(s)
    return (
        f"Interrupt sent but no stop reported within {timeout:g}s. If the Renode emulation is "
        "paused, start it (Renode MCP `start`) so the stub can answer."
    )


@mcp.tool()
async def gdb_step(
    kind: Annotated[
        Literal["step", "next", "stepi", "nexti", "finish"],
        Field(description="step/next: source line (into/over calls); stepi/nexti: one instruction; finish: run until current function returns."),
    ] = "next",
    count: int = 1,
    timeout: Annotated[
        float, Field(description="A step over code that blocks on I/O (e.g. waiting for a Java test to send UART data) may take long.")
    ] = 10.0,
    session: SessionArg = "default",
) -> str:
    """Single-step the CPU (source-line or instruction granularity)."""
    s = _session(session)
    _require_stopped(s)
    mi = {
        "step": "-exec-step",
        "next": "-exec-next",
        "stepi": "-exec-step-instruction",
        "nexti": "-exec-next-instruction",
        "finish": "-exec-finish",
    }[kind]
    if kind == "finish":
        return await _resume_and_maybe_wait(s, mi, True, timeout)
    report = ""
    for i in range(max(1, count)):
        report = await _resume_and_maybe_wait(s, mi, True, timeout)
        if s.gdb.state != "stopped" or (s.gdb.last_stop or {}).get("reason") not in (
            "end-stepping-range",
            None,
        ):
            if i < count - 1:
                report = f"Stopped after {i + 1}/{count} steps:\n{report}"
            break
    return report


@mcp.tool()
async def gdb_run_to(
    location: Annotated[str, Field(description="Function, file:line or *address.")],
    timeout: float = 30.0,
    session: SessionArg = "default",
) -> str:
    """Run until `location` is reached (temporary breakpoint + continue + wait)."""
    s = _session(session)
    _require_stopped(s)
    await _run(s.gdb.command(f"-break-insert -t {mi_quote(location)}"))
    return await _resume_and_maybe_wait(s, "-exec-continue", True, timeout)


# ---------------------------------------------------------------------- breakpoints

AutoHalt = Annotated[
    bool,
    Field(description="If the target is running, briefly interrupt it to apply the change, then resume it."),
]


@mcp.tool()
async def gdb_breakpoint(
    location: Annotated[str, Field(description="Function, file:line, or *0xADDRESS.")],
    condition: Annotated[str | None, Field(description="C expression; stop only when true, e.g. 'len > 64'.")] = None,
    temporary: bool = False,
    hardware: bool = False,
    ignore_count: Annotated[int, Field(description="Skip this many hits before stopping.")] = 0,
    thread: Annotated[int | None, Field(description="Only stop on this GDB thread (= Renode CPU).")] = None,
    auto_halt: AutoHalt = True,
    session: SessionArg = "default",
) -> str:
    """Insert a breakpoint. Can be added while firmware runs (auto_halt)."""
    s = _session(session)
    args = []
    if temporary:
        args.append("-t")
    if hardware:
        args.append("-h")
    if condition:
        args += ["-c", mi_quote(condition)]
    if ignore_count:
        args += ["-i", str(ignore_count)]
    if thread is not None:
        args += ["-p", str(thread)]
    async with _halted(s, auto_halt) as halted:
        r = await _run(s.gdb.command(f"-break-insert -f {' '.join(args)} {mi_quote(location)}"))
    bkpt = r.payload.get("bkpt", {})
    return f"Breakpoint set: {_fmt_bkpt(bkpt)}" + (" (target was briefly halted and resumed)" if halted else "")


@mcp.tool()
async def gdb_watchpoint(
    expression: Annotated[str, Field(description="Variable or lvalue, e.g. 'counter' or '*(uint32_t*)0x20000100'.")],
    access: Annotated[
        Literal["write", "read", "access"], Field(description="Stop on write (default), read, or any access.")
    ] = "write",
    condition: str | None = None,
    auto_halt: AutoHalt = True,
    session: SessionArg = "default",
) -> str:
    """Stop when memory is written/read. Useful to find who corrupts a variable."""
    s = _session(session)
    flag = {"write": "", "read": "-r ", "access": "-a "}[access]
    async with _halted(s, auto_halt):
        r = await _run(s.gdb.command(f"-break-watch {flag}{mi_quote(expression)}"))
        wp = r.payload.get("wpt") or r.payload.get("hw-rwpt") or r.payload.get("hw-awpt") or {}
        num = wp.get("number")
        if condition and num:
            await _run(s.gdb.command(f"-break-condition {num} {condition}"))
    return f"Watchpoint #{num} ({access}) on {wp.get('exp', expression)}" + (f" if {condition}" if condition else "")


@mcp.tool()
async def gdb_dprintf(
    location: Annotated[str, Field(description="Function, file:line, or *0xADDRESS.")],
    format: Annotated[str, Field(description='printf format, e.g. "rx len=%d state=%d\\n".')],
    args: Annotated[list[str] | None, Field(description="C expressions for the format arguments.")] = None,
    condition: str | None = None,
    auto_halt: AutoHalt = True,
    session: SessionArg = "default",
) -> str:
    """Non-stopping log point: print values each time `location` executes, then continue automatically.

    Best tool for observing firmware while Java tests run: output lands in gdb_events
    without leaving the CPU halted. Each hit still costs a short round-trip, so avoid
    very hot code paths.
    """
    s = _session(session)
    fmt = format if format.endswith("\\n") or format.endswith("\n") else format + "\\n"
    fmt = fmt.replace("\n", "\\n").replace('"', '\\"')
    cli = f'dprintf {location},"{fmt}"' + "".join(f",{a}" for a in args or [])
    async with _halted(s, auto_halt):
        r = await _run(s.gdb.console(cli))
        num = None
        for word in r.text.split():
            if word.isdigit():
                num = word
                break
        if condition and num:
            await _run(s.gdb.command(f"-break-condition {num} {condition}"))
    return (r.text or "dprintf set") + "\nOutput appears in gdb_events (kind=console) while the target runs."


@mcp.tool()
async def gdb_breakpoints(session: SessionArg = "default") -> str:
    """List breakpoints, watchpoints and dprintfs with hit counts."""
    s = _session(session)
    r = await _run(s.gdb.command("-break-list"))
    body = r.payload.get("BreakpointTable", {}).get("body", [])
    if not body:
        return "No breakpoints."
    lines = []
    for b in body:
        lines.append(_fmt_bkpt(b))
        for loc in b.get("locations", []) or []:
            lines.append(f"    {loc.get('number')}: {describe_frame(loc)}")
    return "\n".join(lines)


@mcp.tool()
async def gdb_delete_breakpoints(
    numbers: Annotated[list[int] | None, Field(description="Breakpoint numbers; omit to delete all.")] = None,
    auto_halt: AutoHalt = True,
    session: SessionArg = "default",
) -> str:
    """Delete breakpoints/watchpoints/dprintfs."""
    s = _session(session)
    async with _halted(s, auto_halt):
        if numbers:
            await _run(s.gdb.command("-break-delete " + " ".join(map(str, numbers))))
        else:
            await _run(s.gdb.console("delete"))
    return f"Deleted {', '.join(map(str, numbers)) if numbers else 'all breakpoints'}."


@mcp.tool()
async def gdb_enable_breakpoints(
    numbers: list[int],
    enable: bool = True,
    auto_halt: AutoHalt = True,
    session: SessionArg = "default",
) -> str:
    """Enable or disable breakpoints without deleting them."""
    s = _session(session)
    cmd = "-break-enable" if enable else "-break-disable"
    async with _halted(s, auto_halt):
        await _run(s.gdb.command(f"{cmd} {' '.join(map(str, numbers))}"))
    return f"{'Enabled' if enable else 'Disabled'} {', '.join(map(str, numbers))}."


# ---------------------------------------------------------------------- inspection


@mcp.tool()
async def gdb_registers(
    names: Annotated[
        list[str] | None,
        Field(description="Register names (e.g. ['pc','sp','r0','xpsr']); omit for general registers; ['all'] for every register."),
    ] = None,
    session: SessionArg = "default",
) -> str:
    """Read CPU registers of the current thread (CPU)."""
    s = _session(session)
    _require_stopped(s)
    if names == ["all"]:
        cli = "info all-registers"
    else:
        cli = "info registers" + ("".join(f" {n.lstrip('$')}" for n in names) if names else "")
    return (await _run(s.gdb.console(cli))).text


@mcp.tool()
async def gdb_set_register(
    name: str,
    value: Annotated[str, Field(description="Value or expression, e.g. '0x20001000' or '$sp + 8'.")],
    session: SessionArg = "default",
) -> str:
    """Write a CPU register (e.g. move pc to skip code)."""
    s = _session(session)
    _require_stopped(s)
    reg = "$" + name.lstrip("$")
    await _run(s.gdb.console(f"set var {reg} = {value}"))
    return (await _run(s.gdb.console(f"p/x {reg}"))).text


@mcp.tool()
async def gdb_read_memory(
    address: Annotated[str, Field(description="Address or expression: '0x20000000', '&rx_buffer', '$sp'.")],
    length: Annotated[int, Field(ge=1, le=65536)] = 64,
    format: Annotated[
        Literal["hex", "bytes", "words8", "words16", "words32", "words64"],
        Field(description="hex = hexdump with ASCII; wordsN = N-bit values using target endianness."),
    ] = "hex",
    session: SessionArg = "default",
) -> str:
    """Read target memory through GDB. Note: MMIO reads hit Renode peripheral models and may have side effects."""
    s = _session(session)
    _require_stopped(s)
    r = await _run(s.gdb.command(f"-data-read-memory-bytes {mi_quote(address)} {length}"))
    blocks = r.payload.get("memory", [])
    if not blocks:
        return "No memory returned."
    out = []
    for blk in blocks:
        base = int(blk["begin"], 16)
        out.append(_hexdump(base, bytes.fromhex(blk.get("contents", "")), format, s.little_endian))
    return "\n".join(out)


@mcp.tool()
async def gdb_write_memory(
    address: Annotated[str, Field(description="Address or expression.")],
    value: Annotated[str | None, Field(description="Integer to write (decimal or 0x..), encoded with `size` bytes in target endianness.")] = None,
    size: Annotated[Literal[1, 2, 4, 8], Field(description="Byte width for `value`.")] = 4,
    hex_bytes: Annotated[str | None, Field(description="Raw bytes as hex string, e.g. 'deadbeef' (written as-is).")] = None,
    session: SessionArg = "default",
) -> str:
    """Write target memory (either `value`+`size` or raw `hex_bytes`)."""
    s = _session(session)
    _require_stopped(s)
    if (value is None) == (hex_bytes is None):
        raise ToolError("Give exactly one of `value` or `hex_bytes`.")
    if value is not None:
        v = _parse_int(value) & ((1 << (8 * size)) - 1)
        data = v.to_bytes(size, "little" if s.little_endian else "big").hex()
    else:
        data = hex_bytes.replace(" ", "").replace("0x", "")
        bytes.fromhex(data)  # validate
    await _run(s.gdb.command(f"-data-write-memory-bytes {mi_quote(address)} {data}"))
    return f"Wrote {len(data) // 2} bytes at {address}."


@mcp.tool()
async def gdb_evaluate(
    expression: Annotated[
        str,
        Field(description="C expression using firmware symbols: 'sensor', 'buf[3]', '*ctx', 'sizeof(struct foo)', or an assignment 'flag = 1'."),
    ],
    format: Annotated[
        Literal["natural", "x", "d", "u", "t", "c", "a"] | None,
        Field(description="Output format like gdb print/FMT (x=hex, t=binary, a=address/symbol)."),
    ] = None,
    session: SessionArg = "default",
) -> str:
    """Evaluate/print an expression (structs are pretty-printed). Assignments modify target state."""
    s = _session(session)
    _require_stopped(s)
    fmt = f"/{format}" if format and format != "natural" else ""
    return (await _run(s.gdb.console(f"print{fmt} {expression}"))).text


@mcp.tool()
async def gdb_backtrace(
    full: Annotated[bool, Field(description="Include local variables of every frame.")] = False,
    limit: int = 32,
    session: SessionArg = "default",
) -> str:
    """Call stack of the current thread (CPU)."""
    s = _session(session)
    _require_stopped(s)
    return (await _run(s.gdb.console(f"backtrace {'full ' if full else ''}{limit}"))).text


@mcp.tool()
async def gdb_frame(
    index: Annotated[int | None, Field(description="Frame number from gdb_backtrace to select; omit for current.")] = None,
    session: SessionArg = "default",
) -> str:
    """Select a stack frame and show its location, arguments and locals."""
    s = _session(session)
    _require_stopped(s)
    sel = await _run(s.gdb.console("frame" if index is None else f"frame {index}"))
    args = await _run(s.gdb.console("info args"))
    loc = await _run(s.gdb.console("info locals"))
    return f"{sel.text}\n\nArguments:\n{args.text}\n\nLocals:\n{loc.text}"


@mcp.tool()
async def gdb_threads(session: SessionArg = "default") -> str:
    """List GDB threads. In Renode each thread is one CPU of the machine."""
    s = _session(session)
    _require_stopped(s)
    return (await _run(s.gdb.console("info threads"))).text


@mcp.tool()
async def gdb_select_thread(thread: int, session: SessionArg = "default") -> str:
    """Make another thread (Renode CPU) current for registers/backtrace/stepping."""
    s = _session(session)
    _require_stopped(s)
    return (await _run(s.gdb.console(f"thread {thread}"))).text


@mcp.tool()
async def gdb_disassemble(
    location: Annotated[str | None, Field(description="Address/expression to start at (e.g. 'tick', '0x800'); default: around the pc.")] = None,
    count: int = 16,
    session: SessionArg = "default",
) -> str:
    """Disassemble instructions ('=>' marks the current pc)."""
    s = _session(session)
    _require_stopped(s)
    r = await _run(s.gdb.console(f"x/{count}i {location if location else '$pc'}"))
    return r.text


@mcp.tool()
async def gdb_source(
    location: Annotated[str | None, Field(description="Function or file:line; default: current pc.")] = None,
    lines: int = 15,
    session: SessionArg = "default",
) -> str:
    """Show source code around a location (needs the ELF and its sources on this machine)."""
    s = _session(session)
    _require_stopped(s)
    await _run(s.gdb.console(f"set listsize {max(1, lines)}"))
    return (await _run(s.gdb.console(f"list {location}" if location else "list *$pc"))).text


# ---------------------------------------------------------------------- program / Renode sync


@mcp.tool()
async def gdb_load_elf(
    elf: Annotated[str, Field(description="Path to the (rebuilt) ELF.")],
    download: Annotated[
        bool,
        Field(description="Also write its sections into target memory via GDB `load` (sets pc to entry). Usually Renode's `sysbus LoadELF` + reset is preferred."),
    ] = False,
    session: SessionArg = "default",
) -> str:
    """Reload symbols from an ELF (e.g. after rebuilding firmware), optionally downloading it to the target."""
    s = _session(session)
    _require_stopped(s)
    if not os.path.isfile(elf):
        raise ToolError(f"ELF not found: {elf}")
    await _run(s.gdb.command(f"-file-exec-and-symbols {mi_quote(os.path.abspath(elf))}", timeout=60))
    s.elf = elf
    msg = f"Symbols loaded from {elf}."
    if download:
        r = await _run(s.gdb.console("load", timeout=300))
        msg += "\n" + r.text
    return msg


@mcp.tool()
async def gdb_resync(session: SessionArg = "default") -> str:
    """Drop GDB's cached registers/memory and re-read the current location.

    Call after anything changed the CPU behind GDB's back through the Renode MCP:
    `machine Reset`, `cpu PC ...`, `sysbus Write...`, `sysbus LoadELF`.
    """
    s = _session(session)
    _require_stopped(s)
    for c in ("maintenance flush register-cache", "maintenance flush dcache"):
        with contextlib.suppress(GdbError):
            await s.gdb.console(c)
    return await _where(s)


@mcp.tool()
async def gdb_renode_monitor(
    command: Annotated[str, Field(description="Renode Monitor command, e.g. 'sysbus.cpu PC' or 'machine Reset'.")],
    session: SessionArg = "default",
) -> str:
    """Send a command to Renode through GDB's `monitor` channel (qRcmd).

    Fallback only: prefer the Renode MCP for Monitor commands. Support and output depend on
    the Renode version's GDB stub.
    """
    s = _session(session)
    r = await _run(s.gdb.console(f"monitor {command}", timeout=30))
    return r.text or "(no output)"


# ---------------------------------------------------------------------- events / raw


@mcp.tool()
async def gdb_events(
    since: Annotated[int, Field(description="Return events with id > since (use the returned next_since to poll).")] = 0,
    limit: int = 100,
    kinds: Annotated[
        list[Literal["stopped", "running", "console", "target", "log", "notify", "gdb-exit"]] | None,
        Field(description="Filter by kind; dprintf output is 'console'."),
    ] = None,
    session: SessionArg = "default",
) -> str:
    """Asynchronous activity recorded while you were doing other things: stops, dprintf output, target output."""
    s = SESSIONS.get(session)
    if s is None:
        raise ToolError(f"No session '{session}'.")
    evs = [e for e in s.gdb.events if e.id > since and (not kinds or e.kind in kinds)]
    shown = evs[-limit:] if limit > 0 else evs
    nxt = s.gdb.events[-1].id if s.gdb.events else since
    if not shown:
        return f"No new events. next_since={nxt}"
    lines = [
        f"[{e.id}] {time.strftime('%H:%M:%S', time.localtime(e.ts))}.{int(e.ts * 1000) % 1000:03d} {e.kind}: {e.text}"
        for e in shown
    ]
    dropped = len(evs) - len(shown)
    if dropped:
        lines.insert(0, f"({dropped} older matching events omitted; raise limit)")
    lines.append(f"next_since={nxt}")
    return "\n".join(lines)


@mcp.tool()
async def gdb_command(
    command: Annotated[str, Field(description="Any gdb CLI command (e.g. 'info line *0x8000124', 'x/8wx $sp', 'ptype struct foo'). Lines starting with '-' are sent as raw GDB/MI.")],
    timeout: float = 30.0,
    session: SessionArg = "default",
) -> str:
    """Escape hatch: run an arbitrary gdb command and return its output."""
    s = _session(session)
    if command.lstrip().startswith("-"):
        r = await _run(s.gdb.command(command.strip(), timeout))
        out = r.text
        return (out + "\n" if out else "") + f"^{r.cls} {r.payload}"
    r = await _run(s.gdb.console(command, timeout))
    if r.cls == "running":
        return (r.text + "\n" if r.text else "") + "Target resumed; use gdb_wait_for_stop."
    return r.text or "(no output)"


def main() -> None:
    import logging
    import sys

    logging.basicConfig(
        level=os.environ.get("RENODE_GDB_MCP_LOG", "WARNING").upper(),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    mcp.run()
