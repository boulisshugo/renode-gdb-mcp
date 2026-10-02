from renode_gdb_mcp.mi import describe_stop, mi_quote
from renode_gdb_mcp.server import _fmt_bkpt, _hexdump


def test_mi_quote_escapes():
    assert mi_quote('a "b" \\c') == '"a \\"b\\" \\\\c"'


def test_hexdump_formats():
    data = bytes(range(0x41, 0x41 + 20))
    hx = _hexdump(0x20000000, data, "hex", True)
    assert hx.splitlines()[0].startswith("0x20000000: 41 42 43")
    assert "|ABCDEFGHIJKLMNOP|" in hx
    w = _hexdump(0x1000, bytes.fromhex("0700000001020304"), "words32", True)
    assert w == "0x00001000: 0x00000007 0x04030201"
    assert _hexdump(0, bytes.fromhex("12345678"), "words32", False) == "0x00000000: 0x12345678"


def test_describe_stop():
    s = describe_stop(
        {"reason": "breakpoint-hit", "bkptno": "2", "thread-id": "1",
         "frame": {"func": "tick", "file": "main.c", "line": "23", "addr": "0x60"}}
    )
    assert s == "Stopped (breakpoint-hit, breakpoint #2, thread 1) in tick () at main.c:23 [pc=0x60]"


def test_fmt_bkpt():
    b = {"number": "1", "type": "breakpoint", "disp": "keep", "enabled": "y", "addr": "0x60",
         "func": "tick", "file": "main.c", "line": "23", "times": "3", "cond": "counter > 2"}
    assert _fmt_bkpt(b) == "#1 breakpoint (enabled, keep)  tick at main.c:23  addr=0x60  hits=3  if counter > 2"
