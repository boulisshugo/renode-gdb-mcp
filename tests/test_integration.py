"""End-to-end: MCP stdio client -> renode-gdb-mcp -> gdb -> GDB stub.

The stub is Renode when `renode` is on PATH (or RENODE_BIN is set), otherwise QEMU's
lm3s6965evb, which speaks the same remote protocol.  Skipped when neither is available.
"""

import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

FW = Path(__file__).parent / "firmware"
ELF = FW / "demo.elf"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _gdb_available() -> bool:
    return any(shutil.which(g) for g in ("gdb-multiarch", "arm-none-eabi-gdb"))


@pytest.fixture(scope="module")
def elf():
    if not ELF.exists():
        if not shutil.which("arm-none-eabi-gcc"):
            pytest.skip("arm-none-eabi-gcc not available to build test firmware")
        subprocess.run(["make", "-C", str(FW)], check=True)
    return ELF


@pytest.fixture
def stub(elf, tmp_path):
    if not _gdb_available():
        pytest.skip("no ARM-capable gdb")
    port = _free_port()
    renode = os.environ.get("RENODE_BIN") or shutil.which("renode")
    if renode:
        script = tmp_path / "demo.resc"
        script.write_text(
            'mach create "demo"\n'
            f"machine LoadPlatformDescription @{FW / 'demo.repl'}\n"
            f"sysbus LoadELF @{elf}\n"
            f"machine StartGdbServer {port} true\n"
        )
        cmd = [renode, "--disable-xwt", "--console", "--plain", str(script)]
    elif shutil.which("qemu-system-arm"):
        cmd = ["qemu-system-arm", "-M", "lm3s6965evb", "-nographic", "-kernel", str(elf),
               "-S", "-gdb", f"tcp:127.0.0.1:{port}", "-monitor", "none", "-serial", "none"]
    else:
        pytest.skip("neither renode nor qemu-system-arm available")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    yield port
    proc.kill()
    proc.wait()


async def call(session: ClientSession, tool: str, **args) -> str:
    res = await session.call_tool(tool, args)
    text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
    assert not res.isError, f"{tool} failed: {text}"
    return text


async def test_full_debug_flow(stub, elf):
    params = StdioServerParameters(command=sys.executable, args=["-m", "renode_gdb_mcp"], env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = {t.name for t in (await s.list_tools()).tools}
        assert {"gdb_connect", "gdb_continue", "gdb_wait_for_stop", "gdb_dprintf"} <= tools

        out = await call(s, "gdb_connect", port=stub, elf=str(elf))
        assert "Connected" in out and "Stopped" in out

        out = await call(s, "gdb_breakpoint", location="tick")
        assert "#1" in out and "tick" in out

        out = await call(s, "gdb_continue")  # non-blocking
        assert "running" in out
        out = await call(s, "gdb_wait_for_stop", timeout=10)
        assert "breakpoint-hit" in out and "tick" in out

        out = await call(s, "gdb_backtrace")
        assert "tick" in out and "main" in out

        out = await call(s, "gdb_evaluate", expression="sensor")
        assert "id = 7" in out
        out = await call(s, "gdb_read_memory", address="&sensor", length=8, format="words32")
        assert "0x00000007" in out

        out = await call(s, "gdb_step", kind="step", count=2)
        assert "compute" in out
        out = await call(s, "gdb_frame")
        assert "x = " in out
        out = await call(s, "gdb_step", kind="finish")
        assert "tick" in out

        out = await call(s, "gdb_registers", names=["pc", "sp"])
        assert re.search(r"^pc\s+0x", out, re.M)
        await call(s, "gdb_write_memory", address="&counter", value="1000")
        assert "1000" in await call(s, "gdb_evaluate", expression="counter")

        # Log point while running; breakpoint removed so firmware free-runs.
        await call(s, "gdb_delete_breakpoints", numbers=[1])
        out = await call(s, "gdb_dprintf", location="compute", format="x=%d", args=["x"])
        assert "Dprintf" in out
        await call(s, "gdb_continue")
        deadline = time.monotonic() + 10
        events = ""
        while time.monotonic() < deadline and events.count("x=") < 3:
            await asyncio.sleep(0.3)
            events = await call(s, "gdb_events", kinds=["console"])
        assert events.count("x=") >= 3, events

        # Add a breakpoint while the target is running (auto halt + resume).
        out = await call(s, "gdb_breakpoint", location="main.c:23")
        assert "briefly halted" in out
        out = await call(s, "gdb_wait_for_stop", timeout=10)
        assert "breakpoint-hit" in out

        out = await call(s, "gdb_breakpoints")
        assert "dprintf" in out and "hits=" in out
        await call(s, "gdb_delete_breakpoints")

        # Inspection while running must be refused with a helpful hint.
        await call(s, "gdb_continue")
        res = await s.call_tool("gdb_registers", {})
        assert res.isError and "gdb_interrupt" in res.content[0].text
        out = await call(s, "gdb_interrupt")
        assert "Stopped" in out

        out = await call(s, "gdb_watchpoint", expression="counter")
        assert "Watchpoint" in out
        out = await call(s, "gdb_continue", wait=True, timeout=10)
        assert "watchpoint" in out.lower()

        out = await call(s, "gdb_run_to", location="compute")
        assert "compute" in out

        assert "compute" in await call(s, "gdb_resync")
        assert "=>" in await call(s, "gdb_disassemble", count=4)
        assert "compute" in await call(s, "gdb_source")
        assert "int32_t" in await call(s, "gdb_command", command="ptype sensor")

        out = await call(s, "gdb_status")
        assert "stopped" in out
        assert "closed" in await call(s, "gdb_disconnect")
