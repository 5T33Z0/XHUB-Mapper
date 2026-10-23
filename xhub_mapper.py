#!/usr/bin/env python3
"""
xhub_mapper.py - macOS-native ACPI-based USB port mapping tool (XHUB/HUBX method)
Enhanced with live IORegistry introspection to detect active XHCI controllers.

Requirements: macOS and Python 3.8+
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import urllib.request
from dataclasses import dataclass, field

if sys.platform != "darwin":
    sys.exit("ERROR: This tool is optimized exclusively for macOS to enable live IORegistry introspection.")

MAX_PORTS = 15
HUBS = {"RHUB": "XHUB", "HUBN": "HUBX"}
RENAMES = {"XHC1": "SHCI", "EHC1": "EH01", "EHC2": "EH02"}
SUSPECT_CONTROLLERS = set(RENAMES)

PORT_TYPES = {
    "typea": 0x00, "mini-ab": 0x01, "expresscard": 0x02, "usb3-a": 0x03,
    "usb3-b": 0x04, "usb3-micro-b": 0x05, "usb3-micro-ab": 0x06,
    "usb3-power-b": 0x07, "typec-hs": 0x08, "typec-switch": 0x09,
    "typec-noswitch": 0x0A, "internal": 0xFF,
}
TYPE_ALIASES = {"usb2": 0x00, "usb3": 0x03}
TYPE_NAMES = {v: k for k, v in PORT_TYPES.items()}

DEFAULT_PLD_BUFFER = [
    "/* 0000 */  0x81, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,",
    "/* 0008 */  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00",
]

INTERNAL_HINT = ("Built-in devices (Bluetooth, laptop webcam, card reader ...) sit on ports that must be "
                 "set to type 'internal' (0xFF) to operate reliably.")

IASL_URL_MACOS = "https://raw.githubusercontent.com/acidanthera/MaciASL/master/Dist/iasl-stable"


class UserError(Exception):
    """Actionable error for end users."""


# ---------------------------------------------------------------------------
# Live macOS IORegistry Introspection
# ---------------------------------------------------------------------------

@dataclass
class LiveController:
    """Represents a physical XHCI controller discovered from macOS IORegistry."""
    name: str
    acpi_path: str
    pci_address: int            # (device << 16) | function
    pci_debug: str              # e.g. "00:14.0"
    vendor_id: int
    device_id: int
    driver: str


def parse_acpi_plane_path(raw_path: str) -> str:
    """Convert IORegistry acpi-path (IOACPIPlane:/_SB/PCI0@0/XHC@140000) to normalized ACPI."""
    cleaned = raw_path.replace("IOACPIPlane:", "").strip()
    segs = [p.split("@")[0] for p in cleaned.split("/") if p]
    norm_segs = [(s + "____")[:4] for s in segs]
    return "\\" + ".".join(norm_segs)


def get_live_xhci_controllers() -> List[LiveController]:
    """Query IOKit via ioreg plist output to find active XHCI controllers."""
    cmd = ["/usr/sbin/ioreg", "-a", "-r", "-c", "IOPCIDevice", "-l"]
    try:
        raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        devices = plistlib.loads(raw)
    except Exception:
        return []

    active_controllers: List[LiveController] = []

    for dev in devices:
        # Check standard PCI Class Code for xHCI (Base: 0x0C, Sub: 0x03, ProgIF: 0x30)
        # In IOReg plists, class-code is 4 bytes: <30 03 0c 00>
        cc_bytes = dev.get("class-code")
        if not isinstance(cc_bytes, bytes) or len(cc_bytes) < 3:
            continue
        # Check if USB (0x0C, 0x03) and xHCI (0x30)
        if cc_bytes[2] != 0x0C or cc_bytes[1] != 0x03 or cc_bytes[0] != 0x30:
            continue

        raw_acpi = dev.get("acpi-path")
        if not raw_acpi or not isinstance(raw_acpi, str):
            continue

        acpi_path = parse_acpi_plane_path(raw_acpi)
        
        # Extract vendor and device IDs
        vid = int.from_bytes(dev.get("vendor-id", b"\x00\x00")[:2], "little")
        did = int.from_bytes(dev.get("device-id", b"\x00\x00")[:2], "little")

        # Extract PCI BDF
        pcidebug = dev.get("pcidebug", "Unknown")
        pci_addr = 0
        assigned = dev.get("assigned-addresses")
        if isinstance(assigned, bytes) and len(assigned) >= 4:
            # First 4 bytes contain bus/dev/fn configuration
            cfg = int.from_bytes(assigned[:4], "little")
            pci_addr = ((cfg >> 11) & 0x1F) << 16 | ((cfg >> 8) & 0x07)

        # Identify driver attachment
        driver = "AppleUSBXHCI"
        for child in dev.get("IORegistryEntryChildren", []):
            c_name = child.get("IOClass", child.get("CFBundleIdentifier", ""))
            if "USB" in c_name:
                driver = child.get("IORegistryEntryName", c_name)
                break

        name = dev.get("IOName", acpi_path.rpartition(".")[2])
        active_controllers.append(
            LiveController(
                name=name,
                acpi_path=acpi_path,
                pci_address=pci_addr,
                pci_debug=pcidebug,
                vendor_id=vid,
                device_id=did,
                driver=driver,
            )
        )
    return active_controllers


# ---------------------------------------------------------------------------
# Path & Numeric Helpers
# ---------------------------------------------------------------------------

def parse_num(tok: Any) -> Optional[int]:
    if tok is None:
        return None
    if isinstance(tok, int):
        return tok
    s = str(tok).strip()
    named = {"zero": 0, "one": 1, "ones": 0xFFFFFFFFFFFFFFFF}
    if s.lower() in named:
        return named[s.lower()]
    try:
        return int(s, 0)
    except ValueError:
        return None


def to_int(v: Any) -> int:
    return v if isinstance(v, int) else int(str(v).strip(), 0)


def parse_type(value: Any) -> int:
    if isinstance(value, int):
        v = value
    else:
        s = str(value).strip().lower()
        if s in PORT_TYPES:
            return PORT_TYPES[s]
        if s in TYPE_ALIASES:
            return TYPE_ALIASES[s]
        try:
            v = int(s, 0)
        except ValueError:
            raise ValueError(f"unknown port type '{value}'")
    if not (0 <= v <= 0x0A or v == 0xFF):
        raise ValueError(f"port type 0x{v:02X} is reserved")
    return v


def hx(v: Optional[int]) -> Optional[str]:
    return None if v is None else f"0x{v:02X}"


def hx32(v: Optional[int]) -> Optional[str]:
    return None if v is None else f"0x{v:08X}"


def natural_key(p: Union[Path, str]) -> List[Union[int, str]]:
    name = p.name if isinstance(p, Path) else str(p)
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def norm_seg(seg: str) -> str:
    return (seg.strip() + "____")[:4]


def norm_path(raw: str, base: str = "\\") -> str:
    raw = raw.strip()
    if raw.startswith("\\"):
        base_segs: List[str] = []
        raw = raw[1:]
    else:
        base_segs = [s for s in base.lstrip("\\").split(".") if s]
        while raw.startswith("^"):
            raw = raw[1:]
            if base_segs:
                base_segs.pop()
    segs = base_segs + [norm_seg(s) for s in raw.split(".") if s]
    return "\\" + ".".join(segs)


# ---------------------------------------------------------------------------
# AST & ASL Token Parsing
# ---------------------------------------------------------------------------

_MASK_RE = re.compile(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*', re.S)
HDR_RE = re.compile(r"^(Device|Scope)\s*\(\s*([^,()\s]+)\s*\)$")
METHOD_RE = re.compile(r"^Method\s*\(\s*([A-Za-z0-9_]{4})\s*[,)]")
CONST_RE = re.compile(r"\bName\s*\(\s*([A-Za-z0-9_]{4})\s*,\s*(0[xX][0-9A-Fa-f]+|\d+|Zero|One)\s*\)")
ADR_RE = re.compile(r"\bName\s*\(\s*_ADR\s*,\s*([^)\s]+)\s*\)")
DBPN_RE = re.compile(r"\bName\s*\(\s*DBPN\s*,\s*([^)\s]+)\s*\)")
EXT_RE = re.compile(r"\bExternal\s*\(\s*([^,()\s]+)\s*,\s*(\w+)")
DSDT_RE = re.compile(r'DefinitionBlock\s*\(\s*"[^"]*"\s*,\s*"DSDT"')


def mask(src: str) -> str:
    def repl(m):
        t = m.group(0)
        if t.startswith('"'):
            return '""'
        return " " if t.startswith("/*") else ""
    return _MASK_RE.sub(repl, src)


@dataclass
class Node:
    header: str
    open: int
    close: int = -1
    parent: Optional[Node] = None
    children: List[Node] = field(default_factory=list)
    kind: str = "other"
    path: str = ""


def parse_tree(src: str) -> Node:
    root = Node(header="", open=-1, close=len(src))
    stack = [root]
    boundary = 0
    for m in re.finditer(r"[{}]", src):
        pos = m.start()
        if m.group() == "{":
            chunk = src[boundary:pos].rstrip()
            header = chunk.rsplit("\n", 1)[-1].strip()
            node = Node(header=header, open=pos, parent=stack[-1])
            stack[-1].children.append(node)
            stack.append(node)
        elif len(stack) > 1:
            stack.pop().close = pos
        boundary = pos + 1
    for n in stack[1:]:
        n.close = len(src)
    return root


def resolve(node: Node, base: str) -> None:
    m = HDR_RE.match(node.header)
    if m:
        node.kind = m.group(1)
        node.path = norm_path(m.group(2), base)
    else:
        node.path = base
    for c in node.children:
        resolve(c, node.path)


def walk(node: Node):
    yield node
    for c in node.children:
        yield from walk(c)


def own_text(node: Node, src: str) -> str:
    parts, pos = [], node.open + 1
    for c in node.children:
        parts.append(src[pos:c.open])
        pos = c.close + 1
    parts.append(src[pos:node.close])
    return "".join(parts)


def text_wo_devices(node: Node, src: str) -> str:
    parts, pos = [], node.open + 1
    for c in node.children:
        parts.append(src[pos:c.open])
        if c.kind not in ("Device", "Scope"):
            parts.append(text_wo_devices(c, src))
        pos = c.close + 1
    parts.append(src[pos:node.close])
    return "".join(parts)


def device_children(node: Node):
    for c in node.children:
        if c.kind == "Device":
            yield c
        elif c.kind == "other":
            yield from device_children(c)


def externals_of(src: str) -> Dict[str, str]:
    return {norm_path(m.group(1), "\\"): m.group(2) for m in EXT_RE.finditer(src)}


# ---------------------------------------------------------------------------
# ASL Expression Evaluator (Arithmetic & Bitwise)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\s*(0[xX][0-9A-Fa-f]+|\d+|[A-Za-z_\\^][\w.\\^]*|[(),+\-*&|^]|<<|>>)")
_NUM_RE = re.compile(r"0[xX][0-9A-Fa-f]+|\d+")
_NAME_RE = re.compile(r"[A-Za-z_\\^][\w.\\^]*")

_OPS: Dict[str, Callable[[int, int], int]] = {
    "Add": lambda a, b: a + b,
    "Subtract": lambda a, b: a - b,
    "Multiply": lambda a, b: a * b,
    "And": lambda a, b: a & b,
    "Or": lambda a, b: a | b,
    "Xor": lambda a, b: a ^ b,
    "ShiftLeft": lambda a, b: (a << (b & 0x3F)) & 0xFFFFFFFFFFFFFFFF,
    "ShiftRight": lambda a, b: (a >> (b & 0x3F)),
}


def tokenize(text: str) -> Optional[List[str]]:
    text, toks, pos = text.strip(), [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            return None
        toks.append(m.group(1))
        pos = m.end()
    return toks


def return_exprs(body: str) -> List[str]:
    out = []
    for m in re.finditer(r"\bReturn\s*\(", body):
        depth, i = 1, m.end()
        while i < len(body) and depth:
            ch = body[i]
            depth += (ch == "(") - (ch == ")")
            i += 1
        if depth == 0:
            out.append(body[m.end():i - 1])
    return out


def eval_returns(body: str, ctx: Tuple[Dict[str, List[str]], Dict[str, int]], depth: int = 0) -> Optional[Set[int]]:
    exprs = return_exprs(body)
    if not exprs:
        return None
    out = set()
    for e in exprs:
        toks = tokenize(e)
        v = eval_tokens(toks, ctx, depth) if toks else None
        if v is None:
            return None
        out |= v
    return out


def eval_tokens(toks: List[str], ctx: Tuple[Dict[str, List[str]], Dict[str, int]], depth: int = 0) -> Optional[Set[int]]:
    methods, consts = ctx
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take():
        pos[0] += 1
        return toks[pos[0] - 1]

    def call(name: str, args: List[Set[int]]) -> Optional[Set[int]]:
        if name in _OPS and len(args) == 2:
            return {_OPS[name](a, b) for a in args[0] for b in args[1]}
        if name == "Not" and len(args) == 1:
            return {~a & 0xFFFFFFFFFFFFFFFF for a in args[0]}
        if args or depth >= 5 or name not in methods:
            return None
        out = set()
        for body in methods[name]:
            v = eval_returns(body, ctx, depth + 1)
            if v is None:
                return None
            out |= v
        return out or None

    def term() -> Optional[Set[int]]:
        t = peek()
        if t is None:
            return None
        take()
        if t == "(":
            v = expr()
            if v is None or peek() != ")":
                return None
            take()
            return v
        if _NUM_RE.fullmatch(t):
            n = parse_num(t)
            return None if n is None else {n}
        if _NAME_RE.fullmatch(t):
            name = t.rsplit(".", 1)[-1].lstrip("\\^")
            if peek() == "(":
                take()
                args = []
                while peek() not in (")", None):
                    a = expr()
                    if a is None:
                        return None
                    args.append(a)
                    if peek() == ",":
                        take()
                if peek() != ")":
                    return None
                take()
                return call(name, args)
            lit = {"Zero": 0, "One": 1}.get(name, consts.get(name))
            return None if lit is None else {lit}
        return None

    def expr() -> Optional[Set[int]]:
        left = term()
        while left is not None and peek() in ("+", "-", "*", "&", "|", "^", "<<", ">>"):
            op = take()
            right = term()
            if right is None:
                return None
            if op == "+":
                left = {a + b for a in left for b in right}
            elif op == "-":
                left = {a - b for a in left for b in right}
            elif op == "*":
                left = {a * b for a in left for b in right}
            elif op == "&":
                left = {a & b for a in left for b in right}
            elif op == "|":
                left = {a | b for a in left for b in right}
            elif op == "^":
                left = {a ^ b for a in left for b in right}
            elif op == "<<":
                left = {(a << (b & 0x3F)) & 0xFFFFFFFFFFFFFFFF for a in left for b in right}
            elif op == ">>":
                left = {a >> (b & 0x3F) for a in left for b in right}
        return left

    result = expr()
    return result if result is not None and pos[0] == len(toks) else None


# ---------------------------------------------------------------------------
# Ports & Hub Extraction
# ---------------------------------------------------------------------------

def upc_info(node: Node, src: str) -> Tuple[bool, Optional[int], Optional[int]]:
    for c in node.children:
        if "_UPC" in c.header:
            body = c.children[0] if c.children else c
            vals = [parse_num(t) for t in src[body.open + 1:body.close].split(",")]
            if len(vals) >= 2 and vals[0] is not None and vals[1] is not None:
                return True, vals[0], vals[1]
            return True, None, None
    return False, None, None


def pld_info(node: Node, src: str) -> Optional[List[str]]:
    for c in node.children:
        if "_PLD" in c.header:
            body = src[c.open + 1:c.close]
            m = re.search(r"Buffer\s*\([^)]*\)\s*\{([^}]*)\}", body, re.S)
            if m:
                lines = [line.strip() for line in m.group(1).strip().splitlines() if line.strip()]
                return lines or None
    return None


def parse_port(node: Node, src: str, ctx=None) -> Optional[dict]:
    m = ADR_RE.search(own_text(node, src))
    adr = parse_num(m.group(1)) if m else None
    note, cands = "", None
    if adr is None and ctx is not None:
        for c in node.children:
            if re.search(r"\bMethod\s*\(\s*_ADR\b", c.header):
                body = src[c.open + 1:c.close]
                vals = eval_returns(body, ctx)
                if vals:
                    if len(vals) == 1:
                        adr = next(iter(vals))
                        note = "_ADR is computed by the table"
                    else:
                        cands = sorted(vals)
                break
    if adr is None and cands is None:
        vals = {parse_num(x.group(1)) for x in ADR_RE.finditer(text_wo_devices(node, src))} - {None}
        if len(vals) == 1:
            adr, note = vals.pop(), "found inside conditional block"
    if adr is None and cands is None:
        return None
    declared, conn, ptype = upc_info(node, src)
    pld_lines = pld_info(node, src)
    return {
        "name": node.path.rpartition(".")[2], "adr": adr, "conn": conn, "type": ptype,
        "dyn": declared and conn is None, "adr_note": note, "adr_candidates": cands,
        "pld_lines": pld_lines,
    }


def adr_diagnosis(pdefs) -> str:
    devs = [(n, s, l) for n, s, l in pdefs if n.kind == "Device"]
    if not devs:
        return "no Device definition in loaded tables"
    n, s, l = devs[0]
    where = Path(l).name
    for c in n.children:
        if re.search(r"\b(?:Method|Name)\s*\(\s*_ADR\b", c.header):
            return f"{where}: _ADR is computed: " + " ".join(s[c.open + 1:c.close].split())[:90]
    return f"{where}: Device found, but declares no valid _ADR"


def merge_upc(p: dict, entries) -> None:
    if p["conn"] is not None and p.get("pld_lines"):
        return
    for n, s, _ in entries:
        if p["conn"] is None:
            declared, conn, ptype = upc_info(n, s)
            if conn is not None:
                p.update(conn=conn, type=ptype, dyn=False)
            elif declared:
                p["dyn"] = True
        if not p.get("pld_lines"):
            pld = pld_info(n, s)
            if pld:
                p["pld_lines"] = pld


def hub_has_sta(entries) -> bool:
    for h, src, _ in entries:
        if any(re.search(r"\bMethod\s*\(\s*_STA\b", c.header) for c in h.children):
            return True
        if re.search(r"\bName\s*\(\s*_STA\b", own_text(h, src)):
            return True
    return False


def oem_state(p: dict) -> str:
    v = p.get("orig_connectable")
    if v is None:
        return "dyn" if p.get("upc_dynamic") else "?"
    return "on" if to_int(v) else "off"


def oem_on_count(c: dict) -> int:
    return sum(oem_state(p) == "on" for p in c["ports"])


def layout_adr(items: list, p: dict, adr_of: Callable[[dict], Optional[int]]) -> Optional[int]:
    def kind(q):
        mm = re.fullmatch(r"(HS|USR|SS)(\d+)", q["name"].upper())
        return (mm.group(1), int(mm.group(2))) if mm else (None, 0)

    k, n = kind(p)
    if k == "HS":
        return n
    if k is None:
        return None
    hs_vals = [adr_of(q) for q in items if kind(q)[0] == "HS" and adr_of(q) is not None]
    hs_max = max(hs_vals) if hs_vals else max([kind(q)[1] for q in items if kind(q)[0] == "HS"] or [0])
    if k == "USR":
        return hs_max + n
    usr_vals = [adr_of(q) for q in items if kind(q)[0] == "USR" and adr_of(q) is not None]
    usr_end = max(usr_vals) if usr_vals else hs_max + sum(kind(q)[0] == "USR" for q in items)
    return usr_end + n


def infer_adrs(ports: list) -> List[str]:
    def get(q):
        return to_int(q["adr"]) if q.get("adr") else None

    filled = []
    for p in ports:
        if p.get("adr"):
            continue
        val = layout_adr(ports, p, get)
        if val is not None:
            p["adr"] = hx(val)
            p["adr_note"] = "inferred from layout"
            filled.append(p["name"])
    return filled


@dataclass
class Table:
    label: str
    src: str
    nodes: list
    externals: dict
    is_dsdt: bool
    dbpn: Optional[int]

    @property
    def name(self) -> str:
        return Path(self.label).name


def collect_hub(t: Table, n: Node, tables, by_path, preselect: str, ctx) -> dict:
    hub_path = n.path
    ctrl_path, _, seg = hub_path.rpartition(".")
    depth = hub_path.count(".")
    below = hub_path + "."
    entries = by_path[hub_path]

    cond = ""
    anc = n.parent
    while anc is not None:
        if re.match(r"^(If|ElseIf|Else)\b", anc.header):
            cond = " ".join(anc.header.split())[:120]
            break
        anc = anc.parent

    m = ADR_RE.search(own_text(n, t.src))
    hub_adr = (parse_num(m.group(1)) if m else 0) or 0

    ctrl_adr = None
    for h, hsrc, _ in by_path.get(ctrl_path, []):
        if h.kind == "Device":
            mm = ADR_RE.search(own_text(h, hsrc))
            ctrl_adr = parse_num(mm.group(1)) if mm else None
            if ctrl_adr is not None:
                break

    refs = []
    for o in tables:
        if o is t:
            continue
        if (ctrl_path in o.externals or hub_path in o.externals
                or any(p.startswith(below) for p in o.externals)
                or any(x.kind == "Scope" and (x.path == hub_path or x.path.startswith(below))
                       for x in o.nodes)):
            refs.append(o)

    names: Dict[str, Set[str]] = {}

    def add(name, tname):
        names.setdefault(name, set()).add(tname)

    for h, _, hlabel in entries:
        for c in device_children(h):
            add(c.path.rpartition(".")[2], Path(hlabel).name)
    for o in refs:
        for p, typ in o.externals.items():
            if p.startswith(below) and p.count(".") == depth + 1 and typ == "DeviceObj":
                add(p.rpartition(".")[2], o.name)
        for x in o.nodes:
            if x.kind == "Scope" and x.path.startswith(below) and x.path.count(".") == depth + 1:
                add(x.path.rpartition(".")[2], o.name)

    ports = []
    for name in names:
        pdefs = by_path.get(f"{hub_path}.{name}", [])
        port = None
        for pn, psrc, plabel in pdefs:
            if pn.kind == "Device":
                port = parse_port(pn, psrc, ctx)
                if port:
                    port["table"] = Path(plabel).name
                    break
        if port is None:
            port = {"name": name, "adr": None, "conn": None, "type": None, "dyn": False,
                    "adr_note": adr_diagnosis(pdefs), "adr_candidates": None,
                    "table": sorted(names[name])[0], "pld_lines": None}
        merge_upc(port, pdefs)
        ports.append(port)

    for p in ports:
        cands = p.get("adr_candidates")
        if p["adr"] is None and cands:
            want = layout_adr(ports, p, lambda q: q["adr"])
            if want in cands:
                p["adr"] = want
                p["adr_note"] = f"inferred: {hx(want)} matches layout"
            else:
                p["adr_note"] = f"runtime candidate: {', '.join(hx(x) for x in cands)}"
    ports.sort(key=lambda p: (p["adr"] is None, p["adr"] or 0, p["name"]))

    plist = []
    for p in ports:
        name, conn, ot = p["name"], p["conn"], p["type"]
        if ot is not None and (ot <= 0x0A or ot == 0xFF):
            tv = ot
        else:
            tv = 0x03 if name.upper().startswith("SS") else 0x00
        if p["adr"] is None or name.upper().startswith("USR"):
            keep = False
        elif preselect == "all":
            keep = True
        elif preselect == "none":
            keep = False
        else:
            keep = bool(conn)
        plist.append({
            "name": name, "adr": hx(p["adr"]), "keep": keep,
            "type": TYPE_NAMES.get(tv, tv), "label": "",
            "orig_connectable": hx(conn), "orig_type": hx(ot),
            "upc_dynamic": bool(p.get("dyn")),
            "adr_note": p.get("adr_note", ""),
            "table": p["table"],
            "pld_lines": p.get("pld_lines"),
        })
    return {
        "path": ctrl_path, "hub": seg, "new_hub": HUBS[seg],
        "rename": RENAMES.get(ctrl_path.rpartition(".")[2]),
        "in_dsdt": t.is_dsdt,
        "conditional": bool(cond), "condition": cond,
        "hub_adr": hx(hub_adr), "ctrl_adr": hx32(ctrl_adr),
        "hub_has_sta": hub_has_sta(entries),
        "source": t.label,
        "referenced_by": sorted(o.name for o in refs),
        "tables": sorted({t.name} | {o.name for o in refs}),
        "ports": plist,
    }


def scan_sources(sources: List[Tuple[str, str]], preselect: str) -> List[dict]:
    tables, by_path = [], {}
    methods, consts = {}, {}
    for label, text in sources:
        src = mask(text)
        root = parse_tree(src)
        resolve(root, "\\")
        nodes = list(walk(root))
        dm = DBPN_RE.search(text)
        tables.append(Table(label, src, nodes, externals_of(src), bool(DSDT_RE.search(text)),
                            parse_num(dm.group(1)) if dm else None))
        for n in nodes:
            if n.kind in ("Device", "Scope"):
                by_path.setdefault(n.path, []).append((n, src, label))
            mm = METHOD_RE.match(n.header)
            if mm:
                methods.setdefault(mm.group(1), []).append(src[n.open + 1:n.close])
        for cm in CONST_RE.finditer(src):
            val = parse_num(cm.group(2))
            if val is not None:
                consts.setdefault(cm.group(1), val)
    ctx = (methods, consts)

    hub_tables = [t for t in tables if t.is_dsdt] + [t for t in tables if not t.is_dsdt]
    found = {}
    for t in hub_tables:
        for n in t.nodes:
            if n.kind != "Device":
                continue
            ctrl_path, _, seg = n.path.rpartition(".")
            if seg in HUBS and ctrl_path and n.path not in found:
                found[n.path] = collect_hub(t, n, tables, by_path, preselect, ctx)

    result = sorted(found.values(), key=lambda c: (-oem_on_count(c), c["path"]))

    # Live IORegistry correlation: match real active controllers to ACPI tables
    live_controllers = get_live_xhci_controllers()
    for c in result:
        matched_live = next((lc for lc in live_controllers if lc.acpi_path == c["path"]), None)
        if matched_live:
            c["is_live"] = True
            c["live_info"] = f"ACTIVE ({matched_live.pci_debug}, Driver: {matched_live.driver})"
        else:
            c["is_live"] = False
            c["live_info"] = "INACTIVE in IORegistry (Ghost/Unpopulated)"

    bt = next((t.dbpn for t in tables if t.dbpn), None)
    if bt:
        bt_name = f"HS{bt:02d}"
        for c in result:
            if any(p["name"] == bt_name for p in c["ports"]):
                c["bt_hint"] = bt_name
    return result


# ---------------------------------------------------------------------------
# macOS File & Terminal Helpers
# ---------------------------------------------------------------------------

def check_path(raw: str) -> Optional[Path]:
    test = raw.strip()
    if not test:
        return None
    # macOS Terminal paste sanitization: remove quotes, trailing spaces, backslash escapes
    if (test.startswith('"') and test.endswith('"')) or (test.startswith("'") and test.endswith("'")):
        test = test[1:-1]
    test = test.replace("\\ ", " ").replace("\\(", "(").replace("\\)", ")")
    if test.startswith("~"):
        test = os.path.expanduser(test)
    p = Path(test).resolve()
    return p if p.exists() else None


def parse_dropped(raw: str) -> List[Path]:
    p = check_path(raw)
    if p:
        return [p]
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return []
    found = [check_path(t) for t in tokens]
    return [x for x in found if x is not None] if tokens and all(found) else []


def grab(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def head(title: str, width: int = 60) -> None:
    if sys.stdout.isatty():
        os.system("clear")
    print(" " + "#" * width)
    print(" #" + title.center(width - 2) + "#")
    print(" " + "#" * width + "\n")


# ---------------------------------------------------------------------------
# macOS iasl Toolchain
# ---------------------------------------------------------------------------

def tools_dir() -> Path:
    return Path(__file__).resolve().parent / "tools"


def find_iasl(explicit=None) -> Optional[str]:
    cands = [explicit] if explicit else []
    cands.append(str(tools_dir() / "iasl"))
    cands.extend([shutil.which(n) for n in ("iasl", "iasl-stable", "iasl-dev")])
    cands.extend([
        f"/Applications/MaciASL.app/Contents/MacOS/{n}"
        for n in ("iasl62", "iasl-stable", "iasl-dev", "iasl")
    ])
    cands.extend(["/opt/homebrew/bin/iasl", "/usr/local/bin/iasl"])

    for c in cands:
        if c and Path(c).is_file():
            try:
                r = subprocess.run([c, "-v"], capture_output=True, text=True, timeout=5)
                if "version" in (r.stdout + r.stderr).lower():
                    return str(c)
            except Exception:
                pass
    return None


def download_iasl() -> Optional[str]:
    dest = tools_dir() / "iasl"
    dest.parent.mkdir(exist_ok=True)
    print(f"Downloading macOS iasl from {IASL_URL_MACOS} ...")
    try:
        req = urllib.request.Request(IASL_URL_MACOS, headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        dest.chmod(dest.stat().st_mode | 0o111)
        if find_iasl(str(dest)):
            print(f"iasl ready at {dest}")
            return str(dest)
    except Exception as e:
        print(f"! Download failed: {e}")
    return None


class Iasl:
    def __init__(self, explicit=None, auto_download=False):
        self.explicit = explicit
        self.auto = auto_download
        self._path = None

    def get(self) -> Optional[str]:
        if not self._path:
            self._path = find_iasl(self.explicit)
        if not self._path and (self.auto or grab("\nDownload iasl into ./tools? [Y/n] ").strip().lower() in ("", "y")):
            self._path = download_iasl()
        return self._path


# ---------------------------------------------------------------------------
# Loading ACPI Tables
# ---------------------------------------------------------------------------

def table_signature(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size < 36:
            return None
        with path.open("rb") as f:
            hdr = f.read(36)
        return hdr[:4].decode("ascii", errors="replace") if re.fullmatch(rb"[A-Z0-9]{4}", hdr[:4]) else None
    except OSError:
        return None


def gather_tables(path: Path) -> Tuple[List[Path], List[Path]]:
    dsls, tables = [], []
    if path.is_dir():
        for f in sorted(path.iterdir(), key=natural_key):
            if f.is_file():
                if f.suffix.lower() == ".dsl":
                    dsls.append(f)
                elif table_signature(f) in ("DSDT", "SSDT"):
                    tables.append(f)
        if not dsls and not tables and (path / "ACPI").is_dir():
            return gather_tables(path / "ACPI")
    elif path.is_file():
        if path.suffix.lower() == ".dsl":
            dsls.append(path)
        elif table_signature(path) in ("DSDT", "SSDT"):
            tables.append(path)
    return dsls, tables


def decompile_amls(tables: List[Path], iasl: str, workdir: Path):
    copies = []
    for a in sorted(tables, key=lambda t: (0 if table_signature(t) == "DSDT" else 1, natural_key(t))):
        c = workdir / a.name
        shutil.copy2(a, c)
        copies.append(c)

    def present(c):
        want = c.with_suffix(".dsl").name.lower()
        for f in workdir.iterdir():
            if f.name.lower() == want and f.stat().st_size > 0:
                return f
        return None

    subprocess.run([iasl, "-da", "-dl", *[c.name for c in copies]], cwd=str(workdir), capture_output=True)
    for c in [x for x in copies if not present(x)]:
        subprocess.run([iasl, "-dl", c.name], cwd=str(workdir), capture_output=True)

    out_files = [f for f in (present(c) for c in copies) if f]
    failed = [c.name for c in copies if not present(c)]
    return out_files, failed


def load_acpi(paths: List[Path], iasl: Iasl) -> List[Tuple[str, str]]:
    dsls, tables = [], []
    for p in paths:
        d, t = gather_tables(p)
        dsls += d
        tables += t
    if not dsls and not tables:
        raise UserError("No DSDT/SSDT tables found in specified path.")

    have = {f.stem.lower() for f in dsls}
    tables = [t for t in tables if t.stem.lower() not in have]
    if tables:
        iasl_path = iasl.get()
        if not iasl_path:
            raise UserError("Decompilation required, but no functional iasl found.")
        keep_dir = results_dir() / "decompiled"
        keep_dir.mkdir(exist_ok=True)
        print(f"Decompiling with {iasl_path} ...")
        with tempfile.TemporaryDirectory(prefix="xhub_") as tmp:
            converted, failed = decompile_amls(tables, iasl_path, Path(tmp))
            for f in converted:
                dest = keep_dir / f.name
                shutil.copy2(f, dest)
                dsls.append(dest)
        if failed:
            print("! Could not decompile: " + ", ".join(failed))

    dsls.sort(key=lambda f: (0 if f.stem.upper().startswith("DSDT") else 1, natural_key(f)))
    return [(str(f), f.read_text(encoding="utf-8", errors="replace")) for f in dsls]


# ---------------------------------------------------------------------------
# SSDT-XHUB Generation
# ---------------------------------------------------------------------------

def prepare(cfg: dict, emit_disabled: bool):
    errors, warnings, out = [], [], []
    for c in cfg.get("controllers", []):
        path = norm_path(c["path"], "\\")
        hub = c["hub"].upper()
        new_hub = c.get("new_hub") or HUBS.get(hub, "XHUB")

        parsed = []
        for p in c.get("ports", []):
            if p.get("adr") in (None, ""):
                if p.get("keep"):
                    errors.append(f"{path}.{p.get('name')}: Missing _ADR.")
                continue
            parsed.append({
                "name": p.get("name", "PRT_"),
                "adr": to_int(p["adr"]),
                "type": parse_type(p.get("type", "typea")),
                "keep": bool(p.get("keep")),
                "label": str(p.get("label", "")),
                "pld_lines": p.get("pld_lines"),
            })

        kept = [p for p in parsed if p["keep"]]
        if not kept:
            continue
        if len(kept) > MAX_PORTS:
            errors.append(f"{path}: {len(kept)} ports selected (macOS limit is {MAX_PORTS})")

        parent, _, old_name = path.rpartition(".")
        new_name = str(c.get("rename") or "").upper()
        new_path = f"{parent}.{new_name}" if new_name else path

        emitted = kept + ([p for p in parsed if not p["keep"]] if emit_disabled else [])
        out.append({
            "path": new_path, "hub": hub, "new_hub": new_hub,
            "rename": (old_name, new_name) if new_name else None,
            "conditional": bool(c.get("conditional")),
            "hub_adr": to_int(c.get("hub_adr", 0)),
            "ports": [{**p, "connectable": 0xFF if p["keep"] else 0x00} for p in emitted],
            "kept": len(kept),
        })
    return out, errors, warnings


def render_port(p: dict) -> List[str]:
    label = f"  // {p['label']}" if p["label"] else ""
    pld_lines = p.get("pld_lines") or DEFAULT_PLD_BUFFER
    lines = [
        f"        Device ({p['name']})",
        "        {",
        f"            Name (_ADR, 0x{p['adr']:02X}){label}",
        "            Method (_UPC, 0, NotSerialized)",
        "            {",
        "                Return (Package (0x04)",
        "                {",
        f"                    0x{p['connectable']:02X},",
        f"                    0x{p['type']:02X},",
        "                    Zero,",
        "                    Zero",
        "                })",
        "            }",
        "",
        "            Method (_PLD, 0, NotSerialized)",
        "            {",
        "                Return (Package (0x01)",
        "                {",
        f"                    Buffer (0x{len(pld_lines) * 8:02X})",
        "                    {",
    ]
    for pl in pld_lines:
        lines.append(f"                        {pl}")
    lines.extend([
        "                    }",
        "                })",
        "            }",
        "        }",
    ])
    return lines


def sta_method(cond: str, darwin_val: str, oem_val: str, indent: str) -> List[str]:
    return [
        f"{indent}Method (_STA, 0, NotSerialized)",
        f"{indent}{{",
        f"{indent}    If ({cond})",
        f"{indent}    {{",
        f"{indent}        Return ({darwin_val})",
        f"{indent}    }}",
        f"{indent}    Else",
        f"{indent}    {{",
        f"{indent}        Return ({oem_val})",
        f"{indent}    }}",
        f"{indent}}}",
    ]


def render_ssdt(controllers: list, oem_id: str, table_id: str, darwin: str = "osi") -> str:
    cond = "OSDW ()" if darwin == "osdw" else '_OSI ("Darwin")'
    lines = [f'DefinitionBlock ("", "SSDT", 2, "{oem_id}", "{table_id}", 0x00000000)', "{"]
    for c in controllers:
        lines.append(f"    External ({c['path'].lstrip(chr(92))}.{c['hub']}, DeviceObj)")
    if darwin == "osdw":
        lines.append("    External (OSDW, MethodObj)")

    for c in controllers:
        hub_full = f"{c['path']}.{c['hub']}"
        body = [
            "",
            f"    // ---- {c['path']}: {c['hub']} -> {c['new_hub']} ({c['kept']} active ports) ----",
            f"    Scope ({hub_full})",
            "    {",
        ]
        body += sta_method(cond, "Zero", "0x0F", "        ")
        body += [
            "    }", "",
            f"    Device ({c['path']}.{c['new_hub']})",
            "    {",
            f"        Name (_ADR, 0x{c['hub_adr']:02X})"
        ]
        body += sta_method(cond, "0x0F", "Zero", "        ")
        for p in c["ports"]:
            body += [""] + render_port(p)
        body += ["    }"]
        if c.get("conditional"):
            body = [f"    If (CondRefOf ({hub_full}))", "    {"] + [f"    {l}" if l else "" for l in body] + ["    }"]
        lines += body
    lines.append("}\n")
    return "\n".join(lines)


def build_ssdt(cfg: dict, dsl: Path, oem_id="OCLT", table_id="XHUB", emit_disabled=False, darwin="osi"):
    controllers, errors, warnings = prepare(cfg, emit_disabled)
    for w in warnings:
        print(f"! {w}")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return None
    dsl.parent.mkdir(parents=True, exist_ok=True)
    dsl.write_text(render_ssdt(controllers, oem_id, table_id, darwin), encoding="utf-8")
    print(f"Wrote {dsl}")
    return controllers


def compile_dsl(dsl: Path, iasl: Iasl) -> Optional[Path]:
    path = iasl.get()
    if not path:
        return None
    r = subprocess.run([path, dsl.name], cwd=str(dsl.parent), capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout + r.stderr, file=sys.stderr)
        return None
    aml = dsl.with_suffix(".aml")
    return aml if aml.exists() else None


def install_to_efi(aml: Path, oc_dir: Path, renames=()) -> None:
    if (oc_dir / "OC").is_dir():
        oc_dir = oc_dir / "OC"
    cfg_path = oc_dir / "config.plist"
    if not cfg_path.is_file():
        raise UserError(f"config.plist not found in {oc_dir}")
    (oc_dir / "ACPI").mkdir(exist_ok=True)
    shutil.copy2(aml, oc_dir / "ACPI" / aml.name)

    backup = cfg_path.with_name(f"config.plist.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(cfg_path, backup)
    with cfg_path.open("rb") as f:
        cfg = plistlib.load(f)

    acpi = cfg.setdefault("ACPI", {})
    adds = acpi.setdefault("Add", [])
    for e in adds:
        if e.get("Path") == aml.name:
            e["Enabled"] = True
            break
    else:
        adds.append({"Comment": "USB map (XHUB method)", "Enabled": True, "Path": aml.name})

    patches = acpi.setdefault("Patch", [])
    for old, new in renames:
        want = (old.encode("ascii"), new.encode("ascii"))
        for e in patches:
            if (e.get("Find"), e.get("Replace")) == want:
                e["Enabled"] = True
                break
        else:
            patches.append({
                "Base": "", "BaseSkip": 0, "Comment": f"{old} to {new}", "Count": 0, "Enabled": True,
                "Find": want[0], "Limit": 0, "Mask": b"", "OemTableId": b"", "Replace": want[1],
                "ReplaceMask": b"", "Skip": 0, "TableLength": 0, "TableSignature": b""
            })
    with cfg_path.open("wb") as f:
        plistlib.dump(cfg, f, sort_keys=False)
    print(f"Installed {aml.name} to {oc_dir / 'ACPI'}")
    print(f"Updated config.plist (Backup: {backup.name})")


# ---------------------------------------------------------------------------
# Universal JSON Ingestion (Native, USBToolBox, USBMap)
# ---------------------------------------------------------------------------

def normalize_imported_data(doc: Any) -> dict:
    if isinstance(doc, dict) and "controllers" in doc and isinstance(doc["controllers"], list):
        if doc["controllers"] and "identifiers" not in doc["controllers"][0]:
            return doc

    # USBToolBox Schema
    if isinstance(doc, list) or (isinstance(doc, dict) and "controllers" in doc):
        raw_ctrls = doc if isinstance(doc, list) else doc["controllers"]
        ctrls = []
        for i, c in enumerate(raw_ctrls):
            ident = c.get("identifiers", {})
            acpi_raw = ident.get("acpi_path") or c.get("acpi_path") or f"\\_SB.PCI0.XHC{i}"
            acpi_path = norm_path(acpi_raw, "\\")
            ports = []
            for p in c.get("ports", []):
                idx = p.get("index")
                raw_type = p.get("type", p.get("class", 0))
                type_val = parse_type(raw_type) if raw_type is not None else 0
                raw_name = str(p.get("name", f"P{idx}"))
                m_num = re.search(r"\d+", raw_name)
                num = int(m_num.group(0)) if m_num else (idx or 1)
                is_ss = p.get("class") == 3 or type_val in (0x03, 0x0A) or "ss" in raw_name.lower()
                safe_name = f"SS{num:02d}" if (is_ss and "Port" in raw_name) else (f"HS{num:02d}" if "Port" in raw_name else norm_seg(raw_name))
                ports.append({
                    "name": safe_name, "adr": hx(idx), "keep": bool(p.get("selected", True)),
                    "type": TYPE_NAMES.get(type_val, type_val), "label": str(p.get("comment", "")),
                    "orig_connectable": hx(0xFF if p.get("selected") else 0x00), "orig_type": hx(type_val),
                    "upc_dynamic": False, "adr_note": "", "table": "USBToolBox",
                })
            ctrls.append({
                "path": acpi_path, "hub": "RHUB", "new_hub": "XHUB",
                "rename": RENAMES.get(acpi_path.rpartition(".")[2]), "in_dsdt": True,
                "conditional": False, "condition": "", "hub_adr": "0x00", "ctrl_adr": None,
                "hub_has_sta": False, "source": "USBToolBox", "referenced_by": [],
                "tables": ["USBToolBox"], "ports": ports,
            })
        return {"controllers": ctrls}

    # USBMap Plist/JSON Schema
    if isinstance(doc, dict):
        ctrls = []
        for ctrl_key, c_val in doc.items():
            if not isinstance(c_val, dict):
                continue
            acpi_raw = c_val.get("acpi_path") or f"\\_SB.PCI0.{ctrl_key.split('@')[0]}"
            acpi_path = norm_path(acpi_raw, "\\")
            raw_ports = c_val.get("ports", {})
            items = raw_ports.items() if isinstance(raw_ports, dict) else [(str(i), x) for i, x in enumerate(raw_ports)]
            ports = []
            for p_key, p_val in items:
                if not isinstance(p_val, dict):
                    continue
                raw_addr = p_val.get("port", p_key)
                if isinstance(raw_addr, str) and len(raw_addr) == 8:
                    addr = int.from_bytes(bytes.fromhex(raw_addr), "little")
                else:
                    addr = parse_num(raw_addr)
                conn = p_val.get("UsbConnector", p_val.get("connector", 0))
                type_val = parse_type(conn)
                ports.append({
                    "name": p_val.get("name", p_key), "adr": hx(addr),
                    "keep": not bool(p_val.get("disabled", False)) and not p_key.startswith("#"),
                    "type": TYPE_NAMES.get(type_val, type_val),
                    "label": str(p_val.get("comment", p_val.get("Comment", ""))),
                    "orig_connectable": hx(0xFF), "orig_type": hx(type_val),
                    "upc_dynamic": False, "adr_note": "", "table": "USBMap",
                })
            ctrls.append({
                "path": acpi_path, "hub": "RHUB", "new_hub": "XHUB",
                "rename": RENAMES.get(acpi_path.rpartition(".")[2]), "in_dsdt": True,
                "conditional": False, "condition": "", "hub_adr": "0x00", "ctrl_adr": None,
                "hub_has_sta": False, "source": "USBMap", "referenced_by": [],
                "tables": ["USBMap"], "ports": ports,
            })
        if ctrls:
            return {"controllers": ctrls}

    raise UserError("Unsupported port map format.")


def read_ports_json(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        try:
            doc = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            doc = plistlib.loads(raw)
        return normalize_imported_data(doc)
    except Exception as e:
        raise UserError(f"Cannot read {path.name}: {e}")


def write_ports_json(path: Path, controllers: list) -> None:
    doc = {"controllers": controllers}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def results_dir() -> Path:
    p = Path(__file__).resolve().parent / "Results"
    p.mkdir(exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Interactive TUI
# ---------------------------------------------------------------------------

@dataclass
class Session:
    iasl: Iasl
    acpi_label: str = ""
    controllers: list = field(default_factory=list)
    darwin: str = "osi"


def print_controller_list(controllers: list) -> None:
    for n, c in enumerate(controllers, 1):
        ports = c["ports"]
        hs = sum(p["name"].upper().startswith("HS") for p in ports)
        ss = sum(p["name"].upper().startswith("SS") for p in ports)
        sel = sum(bool(p["keep"]) for p in ports)
        live_tag = f"  [{c['live_info']}]" if "live_info" in c else ""
        print(f"  {n:>2}. {c['path']}.{c['hub']}{live_tag}")
        print(f"      {len(ports)} ports (HS {hs}, SS {ss}) | selected {sel}/{MAX_PORTS}")
        if c.get("conditional"):
            print(f"      conditional: {c.get('condition', '')[:70]}")


def edit_controller(c: dict) -> None:
    ports = c["ports"]
    while True:
        sel = sum(bool(p["keep"]) for p in ports)
        head(f"Choose Ports: {c['path']}")
        if "live_info" in c:
            print(f" Status: {c['live_info']}")
        print(f" Selected: {sel}/{MAX_PORTS}" + ("  !! OVER LIMIT !!" if sel > MAX_PORTS else ""))
        print(f"\n  {'#':>3}  {'Port':<5} {'_ADR':<6} {'Use':<4} {'Type':<15} {'OEM':<4} Label")
        for n, p in enumerate(ports, 1):
            print(f"  {n:>3}  {p['name']:<5} {str(p.get('adr') or '?'):<6} [{'x' if p['keep'] else ' '}]  "
                  f"{str(p['type']):<15} {oem_state(p):<4} {p.get('label', '')}")
        print("\n  Commands: k <ports> (toggle), on/off <ports>, t <ports> <type>, a auto, d (done)")
        line = grab("\n Command: ").strip()
        if not line:
            continue
        parts = line.split(None, 1)
        verb, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if verb in ("d", "done"):
            return
        if verb in ("k", "on", "off"):
            idx_list = [int(x) - 1 for x in re.split(r"[,\s]+", rest) if x.isdigit()]
            for i in idx_list:
                if 0 <= i < len(ports):
                    ports[i]["keep"] = (not ports[i]["keep"]) if verb == "k" else (verb == "on")
        elif verb == "t":
            spec, _, tname = rest.rpartition(" ")
            try:
                v = parse_type(tname)
                for i in [int(x) - 1 for x in re.split(r"[,\s]+", spec) if x.isdigit()]:
                    if 0 <= i < len(ports):
                        ports[i]["type"] = TYPE_NAMES.get(v, v)
            except ValueError as e:
                print(f"! {e}")
        elif verb == "a" and rest.strip() == "auto":
            done = infer_adrs(ports)
            print("Inferred: " + ", ".join(done))


def select_acpi(sess: Session) -> bool:
    head("Select ACPI Tables")
    print("Drag and drop your ACPI folder (e.g. SysReport/ACPI) or DSDT/SSDT files.\n")
    raw = grab(" Path: ").strip()
    paths = parse_dropped(raw)
    if not paths:
        return False
    try:
        sources = load_acpi(paths, sess.iasl)
        sess.controllers = scan_sources(sources, "connectable")
        sess.acpi_label = f"{len(sources)} tables loaded"
        return True
    except UserError as e:
        print(f"\n! {e}")
        grab("Press enter to continue...")
        return False


def interactive(iasl_path=None, auto_download=False) -> None:
    sess = Session(iasl=Iasl(iasl_path, auto_download))
    while True:
        head("XHUB Mapper (macOS Edition)")
        print(f" ACPI:   {sess.acpi_label or 'not loaded'}")
        if sess.controllers:
            live_cnt = sum(1 for c in sess.controllers if c.get("is_live"))
            print(f" Hubs:   {len(sess.controllers)} total ({live_cnt} active in live IORegistry)")
        print(f" iasl:   {sess.iasl.get() or 'not found'}")
        print(f" Darwin Check: {'OSDW ()' if sess.darwin == 'osdw' else '_OSI (\"Darwin\")'}\n")
        print(" 1. Select ACPI folder / tables (drag and drop)")
        print(" 2. Choose controllers / ports")
        print(" 3. Generate SSDT-XHUB")
        print(" 4. Save ports.json")
        print(" 5. Load ports.json (or USBToolBox / USBMap files)")
        print(" 6. Toggle Darwin check (_OSI <-> OSDW)")
        print(" Q. Quit\n")
        choice = grab(" Option: ").strip().lower()
        if choice == "1":
            if select_acpi(sess) and sess.controllers:
                head("Controllers Found")
                print_controller_list(sess.controllers)
                grab("\nPress enter to continue...")
        elif choice == "2":
            if not sess.controllers:
                continue
            head("Controllers")
            print_controller_list(sess.controllers)
            idx = grab("\nController number: ").strip()
            if idx.isdigit() and 1 <= int(idx) <= len(sess.controllers):
                edit_controller(sess.controllers[int(idx) - 1])
        elif choice == "3":
            if not sess.controllers:
                continue
            dsl = results_dir() / "SSDT-XHUB.dsl"
            ctrls = build_ssdt({"controllers": sess.controllers}, dsl, darwin=sess.darwin)
            if ctrls:
                aml = compile_dsl(dsl, sess.iasl)
                if aml:
                    print(f"\nCompiled {aml}")
                    raw = grab("Drag EFI/OC folder here to install (or enter to skip): ").strip()
                    if raw:
                        tgt = check_path(raw)
                        if tgt and tgt.is_dir():
                            install_to_efi(aml, tgt, [c["rename"] for c in ctrls if c.get("rename")])
            grab("\nPress enter to continue...")
        elif choice == "4":
            if sess.controllers:
                write_ports_json(results_dir() / "ports.json", sess.controllers)
                print(f"Saved to {results_dir() / 'ports.json'}")
            grab("Press enter to continue...")
        elif choice == "5":
            raw = grab("Drag port map JSON / plist here: ").strip()
            p = check_path(raw)
            if p and p.is_file():
                try:
                    sess.controllers = read_ports_json(p)["controllers"]
                    sess.acpi_label = f"{p.name} (imported)"
                    print("Loaded successfully.")
                except Exception as e:
                    print(f"! {e}")
            grab("Press enter to continue...")
        elif choice == "6":
            sess.darwin = "osdw" if sess.darwin == "osi" else "osi"
        elif choice == "q":
            sys.exit(0)


# ---------------------------------------------------------------------------
# CLI & Verification
# ---------------------------------------------------------------------------

def run_tests():
    print("Running verification suite...")
    # 1. ASL evaluation
    ctx = ({"SSPA": ["Return (0x10)"], "CALC": ["Return (Add (SSPA (), 0x02))"]}, {"BASE": 5})
    toks = tokenize("Add (BASE, CALC ())")
    assert eval_tokens(toks, ctx) == {23}
    # 2. Live IORegistry query verification on macOS
    live = get_live_xhci_controllers()
    print(f"Live IORegistry detected {len(live)} XHCI controller(s) on this Mac.")
    for lc in live:
        print(f" - {lc.name} at {lc.acpi_path} (PCI: {lc.pci_debug}, Driver: {lc.driver})")
    print("All tests passed.")


def main():
    ap = argparse.ArgumentParser(description="macOS ACPI USB port mapping automation tool.")
    sub = ap.add_subparsers(dest="cmd")

    i = sub.add_parser("interactive", help="menu-driven mode (default)")
    i.set_defaults(func=lambda a: interactive())

    s = sub.add_parser("scan", help="scan ACPI tables and correlate with live IORegistry")
    s.add_argument("paths", nargs="*")
    s.add_argument("-o", "--output", default="ports.json")
    s.add_argument("--preselect", choices=["connectable", "all", "none"], default="connectable")
    s.set_defaults(func=lambda a: write_ports_json(Path(a.output), scan_sources(load_acpi([Path(p) for p in a.paths], Iasl()), a.preselect)))

    g = sub.add_parser("generate", help="generate SSDT-XHUB from port mapping file")
    g.add_argument("config", help="path to ports.json / usb.json / USB.plist")
    g.add_argument("-o", "--output", default="SSDT-XHUB.dsl")
    g.add_argument("--compile", action="store_true")
    g.add_argument("--efi", help="path to EFI/OC")
    g.add_argument("--emit-disabled", action="store_true")
    g.add_argument("--darwin-check", choices=["osi", "osdw"], default="osi")
    g.set_defaults(func=lambda a: build_ssdt(read_ports_json(Path(a.config)), Path(a.output), emit_disabled=a.emit_disabled, darwin=a.darwin_check))

    t = sub.add_parser("test", help="run tests and query live IORegistry")
    t.set_defaults(func=lambda a: run_tests())

    args = ap.parse_args()
    if args.cmd is None:
        interactive()
    else:
        args.func(args)


if __name__ == "__main__":
    main()