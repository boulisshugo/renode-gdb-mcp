## renode-gdb-mcp 0.1.0

First release: an MCP server that drives GDB against Renode's GDB stub
(`machine StartGdbServer 3333 true`). It is built to be used alongside a Renode MCP and
an IDE MCP that runs Java tests.

### Highlights
- Non-blocking execution control: `gdb_continue` returns immediately; `gdb_wait_for_stop`,
  `gdb_interrupt`, `gdb_step`, `gdb_run_to`.
- Breakpoints, watchpoints and **non-stopping `gdb_dprintf` log points**, with output
  collected in `gdb_events`.
- Breakpoint changes while running: the CPU is briefly halted, changed, and resumed.
- Inspection: registers, memory (hexdump/words), expressions, backtrace, frames,
  threads (one per Renode CPU), disassembly, source.
- Renode helpers: `gdb_resync` after resets or pokes made through the Renode MCP, and
  `gdb_renode_monitor` via GDB's `monitor` channel.
- Several sessions at once (one per Renode machine/port).

### Install
```bash
pip install renode-gdb-mcp-0.1.0-py3-none-any.whl   # from the release assets
# or unzip renode-gdb-mcp-0.1.0.zip && pip install ./renode-gdb-mcp-0.1.0
```
Requires Python ≥ 3.10 and `gdb-multiarch` (or a cross gdb such as `arm-none-eabi-gdb`).

Tested end to end against QEMU's GDB stub; not yet validated against a real Renode
instance.
