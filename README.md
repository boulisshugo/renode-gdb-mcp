# renode-gdb-mcp

An MCP server that lets an AI agent debug firmware running in
[Renode](https://renode.io) through Renode's built-in GDB stub
(`machine StartGdbServer 3333`).

It is designed to be used **next to** two other MCP servers:

| MCP | Role |
|---|---|
| **Renode MCP** | Drives the Renode Monitor: create the machine, `LoadELF`, `start`/`pause`/`reset`, peripherals |
| **renode-gdb-mcp** (this) | Source-level debugging: breakpoints, stepping, variables, memory, registers, log points |
| **IntelliJ IDEA MCP** | Runs the Java tests that talk to the simulated firmware (UART socket, network, ...) |

Under the hood it runs a real `gdb` (default `gdb-multiarch`) over GDB/MI in
**async mode**, so resuming the CPU never blocks the agent. You can continue the
firmware, start a Java test from the IDE, and then wait for a breakpoint.

## Install

You need Python ≥ 3.10 and a GDB that understands your target, e.g.
`sudo apt install gdb-multiarch`, or `arm-none-eabi-gdb` from the Arm toolchain.

```bash
# run straight from git
uvx --from git+https://github.com/boulisshugo/renode-gdb-mcp renode-gdb-mcp

# or install locally
pip install -e .
```

### Register with your MCP client

Claude Code:

```bash
claude mcp add renode-gdb -- uvx --from git+https://github.com/boulisshugo/renode-gdb-mcp renode-gdb-mcp
```

Or with `.mcp.json` (or any MCP client config):

```json
{
  "mcpServers": {
    "renode-gdb": {
      "command": "renode-gdb-mcp",
      "env": {
        "RENODE_GDB_MCP_GDB": "arm-none-eabi-gdb",
        "RENODE_GDB_PORT": "3333"
      }
    }
  }
}
```

| Env var | Default | Meaning |
|---|---|---|
| `RENODE_GDB_MCP_GDB` | auto (`gdb-multiarch`, `arm-none-eabi-gdb`, `riscv64-unknown-elf-gdb`, …, `gdb`) | GDB executable |
| `RENODE_GDB_HOST` | `localhost` | Default host for `gdb_connect` |
| `RENODE_GDB_PORT` | `3333` | Default port for `gdb_connect` |
| `RENODE_GDB_MCP_LOG` | `WARNING` | Log level (stderr). `DEBUG` shows every MI line |

## Renode side

```resc
mach create "demo"
machine LoadPlatformDescription @tests/firmware/demo.repl
sysbus LoadELF @tests/firmware/demo.elf
machine StartGdbServer 3333 true      # true = start emulation when GDB connects
```

Renode accepts one GDB client per port. For several machines, start one server per
machine on different ports and use a different `session` name in each tool call.

## Typical agent workflow

1. **Renode MCP:** load platform + ELF, `machine StartGdbServer 3333 true`.
2. `gdb_connect(port=3333, elf="build/app.elf")`: the CPU is halted.
3. `gdb_breakpoint("uart_rx_handler", condition="len > 64")` or
   `gdb_dprintf("protocol.c:120", "cmd=%d len=%d", ["cmd", "len"])`.
4. `gdb_continue()`: returns immediately; firmware runs.
5. **IDEA MCP:** run the Java test.
6. `gdb_wait_for_stop(timeout=60)`, then `gdb_backtrace`, `gdb_frame`,
   `gdb_evaluate("*ctx")`, `gdb_read_memory("&rx_buf", 128)`.
7. `gdb_events()` shows dprintf output and every stop that happened in the meantime.

## Tools

| Tool | What it does |
|---|---|
| `gdb_connect` / `gdb_disconnect` / `gdb_status` | Attach to Renode (retries while Renode is still starting the server), detach, show state |
| `gdb_continue` | Resume; **non-blocking by default** (`wait=true` to block with a timeout) |
| `gdb_wait_for_stop` | Wait for a breakpoint / watchpoint / step end, with a timeout |
| `gdb_interrupt` | Halt the CPU (Ctrl-C) |
| `gdb_step` | `step` / `next` / `stepi` / `nexti` / `finish`, with count and timeout |
| `gdb_run_to` | Temporary breakpoint + continue + wait |
| `gdb_breakpoint` | Conditions, ignore counts, temporary, hardware, per-thread (= per-CPU) |
| `gdb_watchpoint` | Stop on write / read / access of a variable or address |
| `gdb_dprintf` | **Log point that doesn't stop the CPU**; output goes to `gdb_events` |
| `gdb_breakpoints` / `gdb_delete_breakpoints` / `gdb_enable_breakpoints` | Manage them, with hit counts |
| `gdb_registers` / `gdb_set_register` | Read/write CPU registers |
| `gdb_read_memory` / `gdb_write_memory` | Hexdump or 8/16/32/64-bit words (target endianness) |
| `gdb_evaluate` | `print` any C expression (structs pretty-printed, `x`/`t`/`a` formats, assignments) |
| `gdb_backtrace` / `gdb_frame` | Call stack; locals and arguments of a frame |
| `gdb_threads` / `gdb_select_thread` | Renode exposes each CPU as a GDB thread |
| `gdb_disassemble` / `gdb_source` | Instructions around pc / source listing |
| `gdb_load_elf` | Reload symbols after a rebuild, optionally `load` into target memory |
| `gdb_resync` | Flush GDB caches after the Renode MCP reset the machine or poked memory/registers |
| `gdb_renode_monitor` | Send a Monitor command via GDB's `monitor` channel (fallback; prefer the Renode MCP) |
| `gdb_events` | Async log: stops, dprintf output, target output; poll with `since` |
| `gdb_command` | Escape hatch: any gdb CLI command, or raw MI if it starts with `-` |

All tools take an optional `session` (default `"default"`).

## Things to know about Renode and GDB together

- **A halted CPU freezes virtual time.** While GDB holds the CPU at a breakpoint,
  firmware timers stop and nothing goes out on the UART. A Java test waiting on a
  reply with a wall-clock timeout will fail. To watch running tests, use
  `gdb_dprintf`, keep halts short, or raise the test's timeouts while debugging.
- **A paused emulation looks like a running target.** If someone runs `pause` through
  the Renode MCP, `gdb_continue` and `gdb_wait_for_stop` just time out. The tools
  mention this in their timeout messages.
- **Changes made behind GDB's back.** After `machine Reset`, `cpu PC …`, or
  `sysbus Write…` through the Renode MCP, call `gdb_resync`.
- **MMIO reads have side effects.** `gdb_read_memory` on a peripheral address performs
  a real bus read on the Renode model (it can clear status flags, pop a FIFO, …). Use
  the Renode MCP to look at peripherals.
- **Changing breakpoints while running.** `gdb_breakpoint`, `gdb_dprintf`, and the delete
  and enable tools briefly interrupt the CPU, apply the change, and resume it
  (`auto_halt=true`). Reading registers or memory while running is refused with a
  hint instead.
- **Detaching** removes the breakpoints. Renode then keeps running or stays paused,
  according to its own emulation state.

## Development

```bash
pip install -e '.[test]'
make -C tests/firmware          # needs arm-none-eabi-gcc
pytest
```

`tests/test_integration.py` runs the whole chain (MCP stdio client → server → gdb →
GDB stub) on a small Cortex-M firmware. It uses Renode when `renode` is on `PATH`
(or `RENODE_BIN` is set). Otherwise it falls back to QEMU's `lm3s6965evb`, which
speaks the same GDB remote protocol and has the same memory map as
`tests/firmware/demo.repl`.
