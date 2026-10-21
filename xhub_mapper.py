#!/usr/bin/env python3
"""
xhub_mapper.py - automates the XHUB/HUBX method for ACPI-based USB port mapping
(https://github.com/5T33Z0/OC-Little-Translated, Content/03_USB_Fixes/ACPI_Mapping_USB_Ports/XHUB_Method)

Two ways to use it
------------------
A) Interactive (default) - run it without arguments, then drag & drop your ACPI
   folder (e.g. OpenCore's SysReport/ACPI) or single DSDT/SSDT files into the
   terminal window and press [enter]:

       python3 xhub_mapper.py

   Menu: load tables -> choose ports -> generate SSDT-XHUB (+compile, +install to EFI).

B) Command line / scripting:

       python3 xhub_mapper.py scan SysReport/ACPI -o ports.json   # (no path = drag & drop prompt)
       python3 xhub_mapper.py generate ports.json --compile
       python3 xhub_mapper.py generate ports.json --efi /Volumes/EFI/EFI/OC
       python3 xhub_mapper.py generate ports.json --darwin-check osdw   # use OSDW () instead of _OSI ("Darwin")

How the tables are analysed
---------------------------
 1. The root hub (RHUB, or HUBN on older controllers) is looked up in the DSDT. Only if no DSDT
    is loaded, all tables are searched.
 2. The other tables (SSDTs) are checked for references to that controller/hub/its ports:
    External (_SB_.PCI0.XHC_, DeviceObj), External (_SB_.PCI0.XHC_.RHUB, DeviceObj),
    External (..RHUB.HS01, DeviceObj) or Scope (..RHUB.HS01) (paths may vary).
 3. ALL ports declared this way (HS/SS/USR/...) are collected, with _ADR/_UPC from wherever they
    are defined. Every port has its OWN _ADR (xHCI numbers the HS and SS lanes of one connector as
    separate ports; they are tied together by _PLD, not by a shared _ADR). An _ADR can be a literal,
    or computed by a method, e.g. Return (Add (SSPA (), Zero)); simple expressions (literals,
    Add/Subtract, + / -, named constants, zero-argument helper methods) are evaluated. If a helper
    can return several values, the one that fits the usual HS -> USR -> SS layout is picked and
    flagged "inferred". Anything still unknown is shown as "?" with a note why.
 4. macOS maps at most 15 ports per controller: with more declared ports you are asked which ones
    you need (the port editor opens; generating is blocked until the selection fits).
 5. Built-in devices (Bluetooth, laptop webcam, ...) need a port of type 'internal' (0xFF); the
    editor and the generator remind you of that.

Drag & drop is nothing magic: the terminal pastes the path as text. check_path()
cleans that text up (quotes, trailing space, backslash-escaped spaces, ~) the way
corpnewt's SSDTTime does it.

iasl: binary tables (.aml) have to be decompiled with iasl. If no working iasl is found,
the script offers to download one into ./tools (same sources SSDTTime uses; pass
--download-iasl to skip the question, --iasl PATH to use your own).

Decompiled tables are kept in Results/decompiled/. Load the tables of ONE system only.

Whether a port is really connected is often decided by firmware variables at runtime (Intel's
xh_* SSDTs use UMAP/UMP3). Those ports show up as "dyn" in the OEM column - the tables can't tell.

Needs Python 3.8+ and the standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_PORTS = 15
HUBS = {"RHUB": "XHUB", "HUBN": "HUBX"}          # OEM hub -> replacement hub
SUSPECT_CONTROLLERS = {"XHC1", "EHC1", "EHC2"}    # names that may need renaming (Step 1)

# _UPC port types (see guide, Step 3)
PORT_TYPES = {
    "typea": 0x00, "mini-ab": 0x01, "expresscard": 0x02, "usb3-a": 0x03,
    "usb3-b": 0x04, "usb3-micro-b": 0x05, "usb3-micro-ab": 0x06,
    "usb3-power-b": 0x07, "typec-hs": 0x08, "typec-switch": 0x09,
    "typec-noswitch": 0x0A, "internal": 0xFF,
}
TYPE_ALIASES = {"usb2": 0x00, "usb3": 0x03}
TYPE_NAMES = {v: k for k, v in PORT_TYPES.items()}

INTERNAL_HINT = ("Built-in devices (Bluetooth, laptop webcam, card reader ...) sit on ports that must be "
                 "set to type 'internal' (0xFF) - they don't work otherwise.")

# iasl download sources (same ones SSDTTime's Scripts/dsdt.py uses)
IASL_URL_MACOS = "https://raw.githubusercontent.com/acidanthera/MaciASL/master/Dist/iasl-stable"
IASL_URL_LINUX = "https://raw.githubusercontent.com/corpnewt/linux_iasl/main/iasl.zip"
ACPICA_RELEASES_PAGE = "https://github.com/open-acpica/acpica/releases"
ACPICA_RELEASES_API = "https://api.github.com/repos/open-acpica/acpica/releases?per_page=10"
ACPICA_BLACKLIST = ("20260408",)      # release SSDTTime skips when picking the Windows build
ACPICA_BINARY_TOOLS = "https://www.intel.com/content/www/us/en/developer/topic-technology/open/acpica/download.html"


class UserError(Exception):
    """A problem the user can act on (bad path, missing iasl, invalid selection...)."""


# --------------------------------------------------------------------------- helpers
def parse_num(tok: str):
    """ASL integer literal -> int, or None if it isn't a plain literal."""
    tok = tok.strip()
    named = {"zero": 0, "one": 1, "ones": 0xFFFFFFFFFFFFFFFF}
    if tok.lower() in named:
        return named[tok.lower()]
    try:
        return int(tok, 0)
    except ValueError:
        return None


def to_int(v) -> int:
    return v if isinstance(v, int) else int(str(v).strip(), 0)


def parse_type(value) -> int:
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


def hx(v):
    return None if v is None else f"0x{v:02X}"


def hx32(v):
    return None if v is None else f"0x{v:08X}"


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def norm_seg(seg: str) -> str:
    return (seg.strip() + "____")[:4]


def norm_path(raw: str, base: str) -> str:
    """Resolve an ASL name against a base scope -> absolute, 4-char-padded path."""
    raw = raw.strip()
    if raw.startswith("\\"):
        base_segs, raw = [], raw[1:]
    else:
        base_segs = [s for s in base.lstrip("\\").split(".") if s]
        while raw.startswith("^"):
            raw = raw[1:]
            if base_segs:
                base_segs.pop()
    segs = base_segs + [norm_seg(s) for s in raw.split(".") if s]
    return "\\" + ".".join(segs)


# --------------------------------------------------------------------------- DSL parsing
_MASK_RE = re.compile(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*', re.S)
HDR_RE = re.compile(r"^(Device|Scope)\s*\(\s*([^,()\s]+)\s*\)$")
METHOD_RE = re.compile(r"^Method\s*\(\s*([A-Za-z0-9_]{4})\s*[,)]")
CONST_RE = re.compile(r"\bName\s*\(\s*([A-Za-z0-9_]{4})\s*,\s*(0[xX][0-9A-Fa-f]+|\d+|Zero|One)\s*\)")
ADR_RE = re.compile(r"\bName\s*\(\s*_ADR\s*,\s*([^)\s]+)\s*\)")
DBPN_RE = re.compile(r"\bName\s*\(\s*DBPN\s*,\s*([^)\s]+)\s*\)")
EXT_RE = re.compile(r"\bExternal\s*\(\s*([^,()\s]+)\s*,\s*(\w+)")
DSDT_RE = re.compile(r'DefinitionBlock\s*\(\s*"[^"]*"\s*,\s*"DSDT"')


def mask(src: str) -> str:
    """Blank out comments and string contents so braces inside them can't confuse us."""
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
    parent: "Node | None" = None
    children: list = field(default_factory=list)
    kind: str = "other"          # Device | Scope | other
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
    """Body of a node without the bodies of its child blocks."""
    parts, pos = [], node.open + 1
    for c in node.children:
        parts.append(src[pos:c.open])
        pos = c.close + 1
    parts.append(src[pos:node.close])
    return "".join(parts)


def text_wo_devices(node: Node, src: str) -> str:
    """Body of a node including If/Else/Method blocks, but without nested Device/Scope blocks."""
    parts, pos = [], node.open + 1
    for c in node.children:
        parts.append(src[pos:c.open])
        if c.kind not in ("Device", "Scope"):
            parts.append(text_wo_devices(c, src))
        pos = c.close + 1
    parts.append(src[pos:node.close])
    return "".join(parts)


def device_children(node: Node):
    """Device blocks directly below node, also those declared inside If/Else blocks."""
    for c in node.children:
        if c.kind == "Device":
            yield c
        elif c.kind == "other":
            yield from device_children(c)


def externals_of(src: str) -> dict:
    """External (path, type) declarations of a table -> {absolute normalized path: type}."""
    return {norm_path(m.group(1), "\\"): m.group(2) for m in EXT_RE.finditer(src)}


# --------------------------------------------------------------------------- tiny ASL integer evaluator
# _ADR is not always a literal: Intel's DSDTs compute it, e.g.  Return (Add (SSPA (), Zero)).
# This evaluates literals, Add/Subtract/Multiply (function style) and + / - (infix), named
# constants (Name (NHSP, 0x0E)) and zero-argument helper methods. Results are SETS of possible
# values: a helper with several Return statements (If/Else branches) yields several candidates.
_TOKEN_RE = re.compile(r"\s*(0[xX][0-9A-Fa-f]+|\d+|[A-Za-z_\\^][\w.\\^]*|[(),+\-])")
_NUM_RE = re.compile(r"0[xX][0-9A-Fa-f]+|\d+")
_NAME_RE = re.compile(r"[A-Za-z_\\^][\w.\\^]*")
_OPS = {"Add": lambda a, b: a + b, "Subtract": lambda a, b: a - b, "Multiply": lambda a, b: a * b}


def tokenize(text: str):
    text, toks, pos = text.strip(), [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            return None
        toks.append(m.group(1))
        pos = m.end()
    return toks


def return_exprs(body: str) -> list:
    """Expressions of all Return (...) statements in a method body (balanced parentheses)."""
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


def eval_returns(body: str, ctx, depth: int = 0):
    """Set of values a method body can return, or None if any Return isn't a computable integer."""
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


def eval_tokens(toks, ctx, depth: int = 0):
    methods, consts = ctx
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take():
        pos[0] += 1
        return toks[pos[0] - 1]

    def call(name, args):
        if name in _OPS and len(args) == 2:
            return {_OPS[name](a, b) for a in args[0] for b in args[1]}
        if args or depth >= 4 or name not in methods:
            return None
        out = set()
        for body in methods[name]:
            v = eval_returns(body, ctx, depth + 1)
            if v is None:
                return None
            out |= v
        return out or None

    def term():
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

    def expr():
        left = term()
        while left is not None and peek() in ("+", "-"):
            op = take()
            right = term()
            if right is None:
                return None
            left = {a + b if op == "+" else a - b for a in left for b in right}
        return left

    result = expr()
    return result if result is not None and pos[0] == len(toks) else None


# --------------------------------------------------------------------------- ports
def upc_info(node: Node, src: str):
    """(declared, connectable, type) of the _UPC declared directly in this block.
    connectable/type are None unless _UPC is a literal package."""
    for c in node.children:
        if "_UPC" in c.header:
            body = c.children[0] if c.children else c   # Name(_UPC, Package{..}) or Method{Return(Package{..})}
            vals = [parse_num(t) for t in src[body.open + 1:body.close].split(",")]
            if len(vals) >= 2 and vals[0] is not None and vals[1] is not None:
                return True, vals[0], vals[1]
            return True, None, None
    return False, None, None


def parse_port(node: Node, src: str, ctx=None):
    """Port data of a Device block. Returns None if no _ADR can be determined at all; a dict with
    adr=None and adr_candidates=[...] if the table computes the _ADR and several values are possible."""
    m = ADR_RE.search(own_text(node, src))
    adr = parse_num(m.group(1)) if m else None
    note, cands = "", None
    if adr is None and ctx is not None:          # _ADR defined as a method (literal or computed)
        for c in node.children:
            if re.search(r"\bMethod\s*\(\s*_ADR\b", c.header):
                body = src[c.open + 1:c.close]
                vals = eval_returns(body, ctx)
                if vals:
                    if len(vals) == 1:
                        adr = next(iter(vals))
                        note = "_ADR is computed by the table: " + " ".join(body.split())[:70]
                    else:
                        cands = sorted(vals)
                break
    if adr is None and cands is None:            # Name (_ADR, literal) inside an If/Else block of the device
        vals = {parse_num(x.group(1)) for x in ADR_RE.finditer(text_wo_devices(node, src))} - {None}
        if len(vals) == 1:
            adr, note = vals.pop(), "found inside a conditional block"
    if adr is None and cands is None:
        return None
    declared, conn, ptype = upc_info(node, src)
    return {"name": node.path.rpartition(".")[2], "adr": adr, "conn": conn, "type": ptype,
            "dyn": declared and conn is None, "adr_note": note, "adr_candidates": cands}


def adr_diagnosis(pdefs) -> str:
    """Why no _ADR could be read for a port (shown to the user)."""
    devs = [(n, s, l) for n, s, l in pdefs if n.kind == "Device"]
    if not devs:
        return "no Device definition in the loaded tables (only referenced)"
    n, s, l = devs[0]
    where = Path(l).name
    for c in n.children:
        if re.search(r"\b(?:Method|Name)\s*\(\s*_ADR\b", c.header):
            return f"{where}: _ADR is computed: " + " ".join(s[c.open + 1:c.close].split())[:90]
    hits = [" ".join(x.split()) for x in re.findall(r"[^\n]*\b_ADR\b[^\n]*", s[n.open + 1:n.close])]
    if hits:
        return f"{where}: _ADR is not a plain number: {hits[0][:90]}"
    return f"{where}: Device found, but it declares no _ADR"


def merge_upc(p: dict, entries) -> None:
    """_UPC is often added to a port from another table (Scope (...RHUB.HS01) in an SSDT) - look there too."""
    if p["conn"] is not None:
        return
    for n, s, _ in entries:
        declared, conn, ptype = upc_info(n, s)
        if conn is not None:
            p.update(conn=conn, type=ptype, dyn=False)
            return
        if declared:
            p["dyn"] = True


def hub_has_sta(entries) -> bool:
    """entries: [(node, masked_src, table_label)] of every block declaring this hub."""
    for h, src, _ in entries:
        if any(re.search(r"\bMethod\s*\(\s*_STA\b", c.header) for c in h.children):
            return True
        if re.search(r"\bName\s*\(\s*_STA\b", own_text(h, src)):
            return True
    return False


def oem_state(p) -> str:
    """'on'/'off' when the OEM table says so literally, 'dyn' when firmware variables decide at
    runtime, '?' when the _UPC form isn't understood."""
    v = p.get("orig_connectable")
    if v is None:
        return "dyn" if p.get("upc_dynamic") else "?"
    return "on" if to_int(v) else "off"


def oem_on_count(c) -> int:
    return sum(oem_state(p) == "on" for p in c["ports"])


def layout_adr(items, p, adr_of):
    """Address the usual Intel xHCI layout gives port p: HSnn = nn, USRn = (last HS) + n,
    SSnn = (last USR, or last HS if there are none) + nn. adr_of(q) returns an int or None."""
    def kind(q):
        mm = re.fullmatch(r"(HS|USR|SS)(\d+)", q["name"].upper())
        return (mm.group(1), int(mm.group(2))) if mm else (None, 0)

    def known_max(kinds):
        vals = [adr_of(q) for q in items if kind(q)[0] in kinds]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    k, n = kind(p)
    if k == "HS":
        return n
    if k is None:
        return None
    hs_max = known_max(("HS",))
    if hs_max is None:
        hs_max = max([kind(q)[1] for q in items if kind(q)[0] == "HS"] or [0])
    if k == "USR":
        return hs_max + n
    usr_end = known_max(("USR",))
    if usr_end is None:
        usr_end = hs_max + sum(kind(q)[0] == "USR" for q in items)
    return usr_end + n


def infer_adrs(ports) -> list:
    """Fill missing addresses of port dicts (hex strings) from the usual layout. A heuristic -
    flagged in the result, has to be verified."""
    def get(q):
        return to_int(q["adr"]) if q.get("adr") else None

    filled = []
    for p in ports:
        if p.get("adr"):
            continue
        value = layout_adr(ports, p, get)
        if value is None:
            continue
        p["adr"] = hx(value)
        p["adr_note"] = "inferred from the port number - verify"
        filled.append(p["name"])
    return filled


@dataclass
class Table:
    label: str
    src: str                 # masked source
    nodes: list
    externals: dict
    is_dsdt: bool
    dbpn: "int | None"

    @property
    def name(self) -> str:
        return Path(self.label).name


def collect_hub(t: Table, n: Node, tables, by_path, preselect: str, ctx) -> dict:
    """Everything known about the root hub n (declared in table t), ports from all tables."""
    hub_path = n.path
    ctrl_path, _, seg = hub_path.rpartition(".")
    depth = hub_path.count(".")
    below = hub_path + "."
    entries = by_path[hub_path]

    m = ADR_RE.search(own_text(n, t.src))
    hub_adr = (parse_num(m.group(1)) if m else 0) or 0

    # _ADR of the controller itself (PCI device/function), to tell the controllers apart
    ctrl_adr = None
    for h, hsrc, _ in by_path.get(ctrl_path, []):
        if h.kind == "Device":
            mm = ADR_RE.search(own_text(h, hsrc))
            ctrl_adr = parse_num(mm.group(1)) if mm else None
            if ctrl_adr is not None:
                break

    # SSDTs that refer to this controller / hub / its ports (External or Scope)
    refs = []
    for o in tables:
        if o is t:
            continue
        if (ctrl_path in o.externals or hub_path in o.externals
                or any(p.startswith(below) for p in o.externals)
                or any(x.kind == "Scope" and (x.path == hub_path or x.path.startswith(below))
                       for x in o.nodes)):
            refs.append(o)

    # ALL declared ports: Device blocks below the hub (any table, also inside If blocks),
    # External declarations and Scope blocks of ports in the referencing tables
    names = {}

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
        if port is None:                       # no readable _ADR anywhere
            port = {"name": name, "adr": None, "conn": None, "type": None, "dyn": False,
                    "adr_note": adr_diagnosis(pdefs), "adr_candidates": None,
                    "table": sorted(names[name])[0]}
        merge_upc(port, pdefs)
        ports.append(port)

    # computed addresses with several possible values: take the one that fits the HS/USR/SS layout
    for p in ports:
        cands = p.get("adr_candidates")
        if p["adr"] is None and cands:
            want = layout_adr(ports, p, lambda q: q["adr"])
            shown = ", ".join(hx(x) for x in cands)
            if want in cands:
                p["adr"] = want
                p["adr_note"] = (f"inferred: the table computes it from runtime data (possible: {shown}); "
                                 f"{hx(want)} fits the HS/USR layout - verify")
            else:
                p["adr_note"] = f"the table computes it from runtime data (possible: {shown})"
    ports.sort(key=lambda p: (p["adr"] is None, p["adr"] or 0, p["name"]))

    plist = []
    for p in ports:
        name, conn, ot = p["name"], p["conn"], p["type"]
        if ot is not None and (ot <= 0x0A or ot == 0xFF):
            tv = ot
        else:
            tv = 0x03 if name.upper().startswith("SS") else 0x00
        if p["adr"] is None or name.upper().startswith("USR"):
            keep = False                           # unknown _ADR / USB-R ports don't work in macOS
        elif preselect == "all":
            keep = True
        elif preselect == "none":
            keep = False
        else:                                       # "connectable"
            keep = bool(conn)
        plist.append({
            "name": name, "adr": hx(p["adr"]), "keep": keep,
            "type": TYPE_NAMES.get(tv, tv), "label": "",
            "orig_connectable": hx(conn), "orig_type": hx(ot),
            "upc_dynamic": bool(p.get("dyn")),
            "adr_note": p.get("adr_note", ""),
            "table": p["table"],
        })
    return {
        "path": ctrl_path, "hub": seg, "new_hub": HUBS[seg],
        "hub_adr": hx(hub_adr), "ctrl_adr": hx32(ctrl_adr),
        "hub_has_sta": hub_has_sta(entries),
        "source": t.label,
        "referenced_by": sorted(o.name for o in refs),
        "tables": sorted({t.name} | {o.name for o in refs}),
        "ports": plist,
    }


def scan_sources(sources, preselect: str):
    tables, by_path = [], {}
    methods, consts = {}, {}                 # for evaluating computed _ADR methods
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

    # step 1: root hubs are looked up in the DSDT
    hub_tables = [t for t in tables if t.is_dsdt]
    if not hub_tables:
        print("! no DSDT among the loaded tables - looking for root hubs in all of them")
        hub_tables = tables

    found = {}
    for t in hub_tables:
        for n in t.nodes:
            if n.kind != "Device":
                continue
            ctrl_path, _, seg = n.path.rpartition(".")
            if seg in HUBS and ctrl_path and n.path not in found:
                found[n.path] = collect_hub(t, n, tables, by_path, preselect, ctx)

    # controllers with the most OEM-enabled ports first (normally the chipset xHCI)
    result = sorted(found.values(), key=lambda c: (-oem_on_count(c), c["path"]))

    # Intel's OEM tables attach their Bluetooth objects to the port named by DBPN
    bt = next((t.dbpn for t in tables if t.dbpn), None)
    if bt:
        bt_name = f"HS{bt:02d}"
        for c in result:
            if any(p["name"] == bt_name for p in c["ports"]):
                c["bt_hint"] = bt_name
    return result


# --------------------------------------------------------------------------- drag & drop input
def check_path(raw: str):
    """
    Turn whatever a terminal pastes on drag & drop into a real path (or None).
    Handles surrounding quotes (Windows / some Linux terminals), a trailing space
    (macOS Terminal), backslash-escaped spaces (macOS/Linux) and a leading ~.
    """
    test, last = raw, None
    while test and test != last:
        last = test
        if os.path.exists(test):
            return Path(os.path.abspath(test))
        if len(test) > 1 and test[0] == test[-1] and test[0] in "\"'":
            test = test[1:-1]
            continue
        if test[0] == "~":
            expanded = os.path.expanduser(test)
            if expanded != test:
                test = expanded
                continue
        if test[0] in " \t":
            test = test[1:]
            continue
        if test[-1] in " \t":
            test = test[:-1]
            continue
        # unescape "\ " etc. but keep an escaped backslash ("\\") as one backslash
        test = "\\".join(x.replace("\\", "") for x in test.split("\\\\"))
    return None


def parse_dropped(raw: str):
    """One dropped path, or several (dragging multiple files pastes them space-separated)."""
    p = check_path(raw)
    if p:
        return [p]
    try:
        tokens = shlex.split(raw, posix=(os.name != "nt"))
    except ValueError:
        return []
    found = [check_path(t) for t in tokens]
    return found if tokens and all(found) else []


def is_admin_windows() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def grab(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""
    except KeyboardInterrupt:
        print()
        raise SystemExit(0)


def head(title: str, width: int = 60) -> None:
    if sys.stdout.isatty():
        if os.name == "nt":
            os.system("cls")
        elif os.environ.get("TERM"):
            os.system("clear")
    print(" " + "#" * width)
    print(" #" + title.center(width - 2) + "#")
    print(" " + "#" * width + "\n")


# --------------------------------------------------------------------------- iasl: find / download
def tools_dir() -> Path:
    return Path(__file__).resolve().parent / "tools"


def iasl_candidates(explicit=None):
    names = ("iasl.exe",) if os.name == "nt" else ("iasl-dev", "iasl-stable", "iasl")
    cands = [explicit] if explicit else []
    cands += [str(tools_dir() / n) for n in names]
    cands += [shutil.which(n) for n in ("iasl", "iasl-stable", "iasl-dev", "iasl.exe")]
    cands += [f"/Applications/MaciASL.app/Contents/MacOS/{n}"
              for n in ("iasl62", "iasl-stable", "iasl-dev", "iasl")]
    return cands


def iasl_works(path: str) -> bool:
    """A binary can exist and still be unusable (wrong architecture, not executable...)."""
    try:
        r = subprocess.run([path, "-v"], capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "version" in ((r.stdout or "") + (r.stderr or "")).lower()


def find_iasl(explicit=None):
    """First iasl that actually runs: --iasl, ./tools, PATH, MaciASL. None if there is none."""
    seen = set()
    for c in iasl_candidates(explicit):
        if not c or c in seen or not Path(c).is_file():
            continue
        seen.add(c)
        if iasl_works(c):
            return str(c)
        print(f"! {c} exists but doesn't run - skipping it", file=sys.stderr)
    return None


def http_get(url: str) -> bytes:
    """urllib first; curl as fallback (python.org builds on macOS often lack CA certificates)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (xhub_mapper)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except Exception as e:
        curl = shutil.which("curl")
        if curl:
            r = subprocess.run([curl, "-fsSL", "--max-time", "120", url], capture_output=True)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        raise UserError(f"download failed ({url}): {e}")


def windows_iasl_url() -> str:
    """Newest non-blacklisted ACPICA release that ships iasl for Windows (GitHub API)."""
    releases = json.loads(http_get(ACPICA_RELEASES_API).decode("utf-8", "replace"))
    for rel in releases:
        label = f"{rel.get('tag_name', '')} {rel.get('name', '')}"
        if any(b in label for b in ACPICA_BLACKLIST):
            continue
        for a in rel.get("assets", []):
            if re.fullmatch(r"iasl-win-.*\.zip|iasl\.exe", a.get("name", ""), re.I):
                return a["browser_download_url"]
        # some releases carry the zip as an attachment link inside the release notes instead
        m = re.search(r"https://github\.com/user-attachments/files/\d+/iasl-win-[^\s)\"'<>]+\.zip",
                      rel.get("body") or "")
        if m:
            return m.group(0)
    raise UserError("no Windows iasl found in the latest ACPICA releases")


def extract_iasl(zpath: Path, dest: Path) -> list:
    got = []
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            base = Path(info.filename).name
            if info.is_dir() or not base.lower().startswith("iasl"):
                continue
            target = dest / base                      # flattened on purpose (no zip-slip)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            got.append(target)
    return got


def download_iasl():
    """Download iasl into ./tools. Returns its path, or None (with manual instructions printed)."""
    dest = tools_dir()
    try:
        dest.mkdir(exist_ok=True)
        if sys.platform == "darwin":
            url = IASL_URL_MACOS
        elif sys.platform.startswith("linux"):
            url = IASL_URL_LINUX
        elif os.name == "nt":
            url = windows_iasl_url()
        else:
            raise UserError("no iasl download known for this OS")
        name = url.rsplit("/", 1)[-1]
        print(f"Downloading {name} ...")
        data = http_get(url)
        if name.lower().endswith(".zip"):
            with tempfile.TemporaryDirectory(prefix="xhub_dl_") as tmp:
                zp = Path(tmp) / name
                zp.write_bytes(data)
                got = extract_iasl(zp, dest)
        else:                                          # macOS: iasl-stable is a bare binary
            target = dest / name
            target.write_bytes(data)
            got = [target]
    except (UserError, OSError, zipfile.BadZipFile, ValueError) as e:
        print(f"! iasl download failed: {e}")
        print("  Get it manually and put it into the 'tools' folder next to this script (or use --iasl PATH):")
        print(f"   macOS:   {IASL_URL_MACOS}")
        print(f"   Linux:   {IASL_URL_LINUX}  (or: apt install acpica-tools)")
        print(f"   Windows: {ACPICA_RELEASES_PAGE}  or  {ACPICA_BINARY_TOOLS}")
        return None
    for p in got:
        if os.name != "nt":
            os.chmod(p, os.stat(p).st_mode | 0o111)
    for p in got:
        if iasl_works(str(p)):
            print(f"iasl ready: {p}")
            return str(p)
    print("! The downloaded iasl doesn't run on this system (wrong architecture? e.g. Apple Silicon")
    print("  without Rosetta). Install one via a package manager instead: brew install acpica /")
    print("  apt install acpica-tools, then use --iasl PATH.")
    return None


class Iasl:
    """Lazy iasl lookup: use a working copy if there is one, otherwise offer to download it."""

    def __init__(self, explicit=None, auto_download=False):
        self.explicit = explicit
        self.auto = auto_download
        self._path = None
        self._offered = False

    def peek(self):
        """Path of a working iasl without downloading anything."""
        if not self._path:
            self._path = find_iasl(self.explicit)
        return self._path

    def get(self):
        return self.peek() or self.download()

    def download(self):
        """Ask (unless --download-iasl) and download. Only offered once per run."""
        if self._offered:
            return None
        self._offered = True
        ok = self.auto
        if not ok and sys.stdin.isatty():
            answer = grab("\n iasl is missing or doesn't work here. Download it from GitHub into the\n"
                          " 'tools' folder next to this script? [Y/n] ")
            ok = answer.strip().lower() in ("", "y", "yes")
        if ok:
            path = download_iasl()
            if path:
                self._path = path
                return path
        return None


# --------------------------------------------------------------------------- loading ACPI tables
def table_signature(path: Path):
    """4-char signature of an ACPI table file (checked via its header), else None."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            hdr = f.read(36)
    except OSError:
        return None
    if len(hdr) < 36 or not re.fullmatch(rb"[A-Z0-9]{4}", hdr[:4]):
        return None
    length = int.from_bytes(hdr[4:8], "little")
    if length < 36 or length > size:
        return None
    return hdr[:4].decode("ascii")


def gather_tables(path: Path):
    """-> (dsl_files, binary_tables) for a dropped file or folder (DSDT/SSDT only)."""
    dsls, tables = [], []
    if path.is_dir():
        for f in sorted(path.iterdir(), key=natural_key):
            if not f.is_file():
                continue
            if f.suffix.lower() == ".dsl":
                dsls.append(f)
            elif table_signature(f) in ("DSDT", "SSDT"):
                tables.append(f)
        if not dsls and not tables and (path / "ACPI").is_dir():
            return gather_tables(path / "ACPI")        # SysReport folder was dropped
    elif path.is_file():
        if path.suffix.lower() == ".dsl":
            dsls.append(path)
        elif table_signature(path) in ("DSDT", "SSDT"):
            tables.append(path)
    return dsls, tables


def decompile_amls(tables, iasl: str, workdir: Path):
    """
    Disassemble binary tables inside workdir (plain file names, cwd = workdir, so it doesn't matter
    where an iasl build puts its output). Same strategy as SSDTTime: all tables in one call with
    '-da -dl' (single namespace, cross-table references resolve), then progressively simpler calls
    for whatever is still missing. Returns ([dsl paths], [(name, args, iasl output)]).
    """
    copies = []
    for a in sorted(tables, key=lambda t: (0 if table_signature(t) == "DSDT" else 1, natural_key(t))):
        c = workdir / a.name
        shutil.copy2(a, c)
        copies.append(c)

    state = {c.name: ([], "") for c in copies}      # name -> (last arguments, last iasl output)

    def present(c):
        want = c.with_suffix(".dsl").name.lower()
        for f in workdir.iterdir():
            if f.name.lower() == want and f.stat().st_size > 0:
                return f
        return None

    def run(args, involved) -> bool:
        try:
            r = subprocess.run([iasl, *args], cwd=str(workdir), capture_output=True,
                               text=True, errors="replace")
            out = (r.stdout or "") + (r.stderr or "")
            ok = True
        except OSError as e:
            out, ok = f"could not run iasl: {e}", False
        for n in involved:
            state[n] = (args, out)
        return ok

    def missing():
        return [c for c in copies if not present(c)]

    if run(["-da", "-dl", *[c.name for c in copies]], [c.name for c in copies]):
        todo = missing()
        if todo:                                      # without the single-namespace mode
            run(["-dl", *[c.name for c in todo]], [c.name for c in todo])
        for c in missing():                           # one by one
            run(["-dl", c.name], [c.name])
        for c in missing():                           # last resort: other tables as externals
            others = [o.name for o in copies if o != c]
            if others:
                run(["-e", *others, "-d", c.name], [c.name])

    out_files = [f for f in (present(c) for c in copies) if f]
    failed = [(c.name, state[c.name][0], state[c.name][1]) for c in copies if not present(c)]
    return out_files, failed


def decompile_to(tables, iasl: str, keep_dir: Path):
    """Decompile in a temp folder and keep the resulting .dsl files in keep_dir."""
    with tempfile.TemporaryDirectory(prefix="xhub_") as tmp:
        converted, failed = decompile_amls(tables, iasl, Path(tmp))
        kept = []
        for f in converted:
            dest = keep_dir / f.name
            shutil.copy2(f, dest)
            kept.append(dest)
    return kept, failed


def load_acpi(paths, iasl: Iasl):
    """Dropped files/folders -> [(label, dsl_text)]. Raises UserError with a readable message."""
    dsls, tables = [], []
    for p in paths:
        d, t = gather_tables(p)
        dsls += d
        tables += t
    if not dsls and not tables:
        raise UserError("No DSDT/SSDT tables or .dsl files found there.")
    have = {f.stem.lower() for f in dsls}
    tables = [t for t in tables if t.stem.lower() not in have]    # prefer an existing .dsl
    dsdts = [t.name for t in tables if table_signature(t) == "DSDT"]
    if len(dsdts) > 1:
        raise UserError("Multiple files with a DSDT signature: " + ", ".join(dsdts) +
                        "\nKeep only one and try again.")
    print(f"Found {len(tables)} binary table(s) and {len(dsls)} .dsl file(s).")
    failed, iasl_path = [], None
    if tables:
        iasl_path = iasl.get()
        if not iasl_path:
            raise UserError(
                "Binary tables have to be decompiled, but no working iasl is available.\n"
                "Accept the download (or use --download-iasl), install it yourself\n"
                "(macOS: brew install acpica; Linux: apt install acpica-tools; Windows: ACPICA\n"
                "tools from acpica.org) and point to it with --iasl PATH.\n"
                "Dropping already decompiled .dsl files works without iasl.")
        keep_dir = results_dir() / "decompiled"
        keep_dir.mkdir(exist_ok=True)
        print(f"Decompiling with {iasl_path} ...")
        kept, failed = decompile_to(tables, iasl_path, keep_dir)
        if not kept:                                  # the iasl we found decompiled nothing - try a fresh one
            alt = iasl.download()
            if alt and alt != iasl_path:
                print("\nRetrying with the downloaded iasl ...")
                iasl_path = alt
                kept, failed = decompile_to(tables, iasl_path, keep_dir)
        dsls += kept
        if kept:
            print(f"Decompiled tables saved to {keep_dir}")
    if failed:
        print("\n! Could not decompile: " + ", ".join(n for n, _, _ in failed))
    if not dsls:
        name, args, output = failed[0]
        tail = "\n".join(output.strip().splitlines()[-8:]) or "(iasl printed nothing)"
        raise UserError("None of the tables could be decompiled.\n"
                        f"iasl:     {iasl_path}\n"
                        f"Command:  iasl {' '.join(args)}   (first failure: {name}, run in a temp folder)\n"
                        f"iasl said:\n{tail}")
    if failed:
        name, _, output = failed[0]
        tail = (output.strip().splitlines() or ["(iasl printed nothing)"])[-1]
        print(f"  first failure: {name}: {tail}")
    dsls.sort(key=lambda f: (0 if f.stem.upper().startswith("DSDT") else 1, natural_key(f)))
    texts = [(str(f), f.read_text(encoding="utf-8", errors="replace")) for f in dsls]
    # decompiled .dsl files are not covered by the binary-table check above, so look for several DSDTs here
    dsdt_files = [Path(label).name for label, text in texts if DSDT_RE.search(text)]
    if len(dsdt_files) > 1:
        raise UserError("More than one DSDT was loaded (" + ", ".join(dsdt_files) + ").\n"
                        "Tables of different machines or dumps would be merged into one namespace and show up as "
                        "extra USB controllers. Drop only the tables of ONE system.")
    return texts


# --------------------------------------------------------------------------- generation
def prepare(cfg: dict, emit_disabled: bool):
    errors, warnings, out, untouched = [], [], [], []
    for c in cfg.get("controllers", []):
        try:
            path = norm_path(c["path"], "\\")
            hub = c["hub"].upper()
        except KeyError as e:
            errors.append(f"controller entry without {e}")
            continue
        if hub not in HUBS:
            errors.append(f"{path}: hub must be RHUB or HUBN, got '{hub}'")
            continue
        new_hub = c.get("new_hub") or HUBS[hub]

        parsed, inferred = [], []
        for p in c.get("ports", []):
            nm = p.get("name", "?")
            if p.get("adr") in (None, ""):
                if p.get("keep"):
                    why = p.get("adr_note") or "not found in the loaded tables"
                    errors.append(f"{path}.{hub}.{nm}: _ADR unknown ({why}) - "
                                  "use 'a auto' or 'a <port> <adr>' in the port editor, "
                                  "or set \"adr\" in ports.json")
                continue
            if p.get("keep") and str(p.get("adr_note", "")).startswith("inferred"):
                inferred.append(nm)
            try:
                parsed.append({
                    "name": nm, "adr": to_int(p["adr"]),
                    "type": parse_type(p.get("type", "typea")),
                    "keep": bool(p.get("keep")), "label": str(p.get("label", "")).replace("\n", " "),
                })
            except (KeyError, ValueError) as e:
                errors.append(f"{path}.{hub}.{nm}: {e}")

        kept = [p for p in parsed if p["keep"]]
        if not kept:
            untouched.append(f"{path}.{hub}")
            continue
        if len(kept) > MAX_PORTS:
            errors.append(f"{path}: {len(kept)} ports selected, macOS allows max. {MAX_PORTS} per controller")
        if inferred:
            warnings.append(f"{path}: _ADR of {', '.join(inferred)} was inferred (not read literally from a "
                            "table) - verify it (DSDT, IORegistryExplorer)")

        emitted = kept + ([p for p in parsed if not p["keep"]] if emit_disabled else [])
        for key in ("name", "adr"):
            seen = set()
            for p in emitted:
                if p[key] in seen:
                    errors.append(f"{path}: duplicate port {key} {p[key]!r}")
                seen.add(p[key])

        # an HSnn/SSnn pair that both use a Type-C type normally shares one type
        by_name = {p["name"].upper(): p for p in kept}
        for name, p in by_name.items():
            mm = re.fullmatch(r"HS(\d+)", name)
            q = by_name.get("SS" + mm.group(1)) if mm else None
            if q and 0x08 <= p["type"] <= 0x0A and 0x08 <= q["type"] <= 0x0A and p["type"] != q["type"]:
                warnings.append(f"{path}: {p['name']} (0x{p['type']:02X}) and {q['name']} (0x{q['type']:02X}) "
                                "look like one Type-C connector but use different types - both normally "
                                "share one (0x09/0x0A); please check")

        if c.get("hub_has_sta"):
            warnings.append(f"{path}.{hub} already has an _STA method in the OEM table - "
                            "the override in this SSDT will conflict with it. Handle that first.")
        if path.rpartition(".")[2] in SUSPECT_CONTROLLERS:
            warnings.append(f"{path}: controller name may clash with Apple's built-in USB maps - "
                            "check Step 1 (renaming) of the guide.")
        out.append({
            "path": path, "hub": hub, "new_hub": new_hub,
            "hub_adr": to_int(c.get("hub_adr", 0)),
            "ports": [{**p, "connectable": 0xFF if p["keep"] else 0x00} for p in emitted],
            "kept": len(kept),
        })

    if untouched and out:
        warnings.append(f"{len(untouched)} controller(s) without selected ports are left untouched: "
                        + ", ".join(untouched))
    if out and not any(p["type"] == 0xFF for c in out for p in c["ports"] if p["keep"]):
        warnings.append("no port is set to type 'internal' (0xFF). Built-in devices (Bluetooth, laptop "
                        "webcam, ...) need one - ignore this on a machine without such devices")
    if not out and not errors:
        errors.append("nothing to generate - select at least one port (\"keep\": true)")
    return out, errors, warnings


def render_port(p) -> list:
    label = f"  // {p['label']}" if p["label"] else ""
    return [
        f"        Device ({p['name']})",
        "        {",
        f"            Name (_ADR, 0x{p['adr']:02X}){label}",
        "            Method (_UPC, 0, NotSerialized)  // _UPC: USB Port Capabilities",
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
        "            Method (_PLD, 0, NotSerialized)  // _PLD: Physical Location of Device",
        "            {",
        "                Return (Package (0x01)",
        "                {",
        "                    Buffer (0x10)",
        "                    {",
        "                        /* 0000 */  0x81, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,",
        "                        /* 0008 */  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00",
        "                    }",
        "                })",
        "            }",
        "        }",
    ]


def sta_method(cond: str, darwin_value: str, other_value: str, indent: str) -> list:
    body = [
        "Method (_STA, 0, NotSerialized)  // _STA: Status",
        "{",
        f"    If ({cond})",
        "    {",
        f"        Return ({darwin_value})",
        "    }",
        "    Else",
        "    {",
        f"        Return ({other_value})",
        "    }",
        "}",
    ]
    return [indent + line for line in body]


def render_ssdt(controllers, oem_id: str, table_id: str, darwin: str = "osi") -> str:
    cond = "OSDW ()" if darwin == "osdw" else '_OSI ("Darwin")'
    L = [f'DefinitionBlock ("", "SSDT", 2, "{oem_id}", "{table_id}", 0x00000000)', "{"]
    for c in controllers:
        L.append(f"    External ({c['path'].lstrip(chr(92))}.{c['hub']}, DeviceObj)")
    if darwin == "osdw":
        L.append("    External (OSDW, MethodObj)")
    for c in controllers:
        L += ["",
              f"    // ---- {c['path']}: {c['hub']} -> {c['new_hub']} ({c['kept']} active ports) ----",
              f"    Scope ({c['path']}.{c['hub']})",
              "    {"]
        L += sta_method(cond, "Zero", "0x0F", "        ")      # OEM hub off under macOS
        L += ["    }", "",
              f"    Device ({c['path']}.{c['new_hub']})",
              "    {",
              f"        Name (_ADR, 0x{c['hub_adr']:02X})"]
        L += sta_method(cond, "0x0F", "Zero", "        ")      # new hub on under macOS only
        for p in c["ports"]:
            L += [""] + render_port(p)
        L += ["    }"]
    L.append("}")
    return "\n".join(L) + "\n"


def build_ssdt(cfg: dict, dsl: Path, oem_id="OCLT", table_id="XHUB", emit_disabled=False, darwin="osi"):
    """Validate cfg, write the .dsl and print a summary. Returns the controllers or None on error."""
    controllers, errors, warnings = prepare(cfg, emit_disabled)
    if darwin == "osdw":
        warnings.append("OSDW () has to exist in your ACPI (native on some Dell DSDTs, otherwise "
                        "SSDT-OSDW) - without it this SSDT won't load properly")
    for w in warnings:
        print(f"! {w}")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return None
    dsl.parent.mkdir(parents=True, exist_ok=True)
    dsl.write_text(render_ssdt(controllers, oem_id, table_id, darwin), encoding="utf-8")
    for c in controllers:
        print(f"{c['path']}: {c['hub']} -> {c['new_hub']}, {c['kept']} active ports")
        for p in c["ports"]:
            if p["keep"]:
                print(f"   {p['name']}  adr 0x{p['adr']:02X}  type 0x{p['type']:02X}  {p['label']}")
    print(f"\nWrote {dsl}")
    return controllers


def compile_dsl(dsl: Path, iasl: Iasl):
    path = iasl.get()
    if not path:
        print("! no working iasl - open the .dsl in MaciASL and save as .aml, "
              "or use --iasl PATH / --download-iasl", file=sys.stderr)
        return None
    # run next to the source with a plain file name (see decompile_amls for why)
    r = subprocess.run([path, dsl.name], cwd=str(dsl.resolve().parent),
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        print(r.stdout + r.stderr, file=sys.stderr)
        print("! iasl reported errors - no .aml produced", file=sys.stderr)
        return None
    aml = dsl.with_suffix(".aml")
    return aml if aml.exists() else None


def install_to_efi(aml: Path, oc_dir: Path) -> None:
    if (oc_dir / "OC").is_dir():          # user passed the EFI folder
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
    adds = cfg.setdefault("ACPI", {}).setdefault("Add", [])
    for e in adds:
        if e.get("Path") == aml.name:
            e["Enabled"] = True
            break
    else:
        adds.append({"Comment": "USB map (XHUB method)", "Enabled": True, "Path": aml.name})
    with cfg_path.open("wb") as f:
        plistlib.dump(cfg, f, sort_keys=False)
    print(f"  copied {aml.name} -> {oc_dir / 'ACPI'}")
    print(f"  config.plist updated (backup: {backup.name})")


# --------------------------------------------------------------------------- ports.json
def write_ports_json(path: Path, controllers) -> None:
    doc = {
        "_help": {
            "keep": "true = port is defined in the new hub (max. 15 per controller). "
                    "A controller without any keep=true port is left untouched.",
            "type": "typea | usb3-a | typec-hs | typec-switch | typec-noswitch | internal | "
                    "mini-ab | expresscard | usb3-b | usb3-micro-b | usb3-micro-ab | "
                    "usb3-power-b | or a number like 0x0A",
            "adr": "null/missing = _ADR not determinable from the tables (see adr_note); set it before using keep=true",
            "label": "free text, ends up as a comment in the SSDT",
            "orig_*, upc_dynamic, adr_note, table, tables, referenced_by, ctrl_adr, bt_hint":
                "information from the OEM tables (read-only)",
        },
        "controllers": controllers,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def read_ports_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UserError(f"cannot read {path}: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("controllers"), list):
        raise UserError(f"{path} has no \"controllers\" list - is it a ports.json from 'scan'?")
    return doc


def pci_label(c) -> str:
    """PCI device.function from the controller's _ADR, e.g. 0x00140000 -> 14.0"""
    v = c.get("ctrl_adr")
    if v is None:
        return "?"
    try:
        adr = to_int(v)
    except ValueError:
        return "?"
    return f"{(adr >> 16) & 0xFFFF:02X}.{adr & 0xFFFF:X}"


def print_controller_list(controllers) -> None:
    for n, c in enumerate(controllers, 1):
        ports = c["ports"]
        hs = sum(p["name"].upper().startswith("HS") for p in ports)
        ss = sum(p["name"].upper().startswith("SS") for p in ports)
        sel = sum(bool(p["keep"]) for p in ports)
        refs = ", ".join(c.get("referenced_by") or []) or "-"
        print(f"  {n:>2}. {c['path']}.{c['hub']}")
        print(f"      PCI {pci_label(c)} | {len(ports)} ports declared (HS {hs}, SS {ss}) | "
              f"OEM-enabled {oem_on_count(c)} | selected {sel}/{MAX_PORTS}")
        print(f"      hub in {Path(c.get('source', '?')).name} | referenced by: {refs}")


def describe(controllers) -> None:
    print(f"{len(controllers)} USB controller(s) with a root hub found in the DSDT, sorted by OEM-enabled ports:\n")
    print_controller_list(controllers)
    print(f"\n  Note: {INTERNAL_HINT}")
    for n, c in enumerate(controllers, 1):
        ports = c["ports"]
        hs = sum(p["name"].upper().startswith("HS") for p in ports)
        ss = sum(p["name"].upper().startswith("SS") for p in ports)
        dyn = sum(oem_state(p) == "dyn" for p in ports)
        unk = sum(oem_state(p) == "?" for p in ports)
        no_adr = [p for p in ports if not p.get("adr")]
        inferred = [p for p in ports if str(p.get("adr_note", "")).startswith("inferred")]
        notes = []
        if len(ports) > MAX_PORTS:
            notes.append(f"{len(ports)} ports are declared - macOS maps at most {MAX_PORTS} per controller, "
                         "you will be asked which ones you need")
        if hs and not ss:
            notes.append("no SS ports found - they may be declared in an SSDT that doesn't reference this "
                         "hub, or use names I don't know: grep -n \"SS01\" Results/decompiled/*.dsl")
        if no_adr:
            notes.append("declared, but no _ADR could be determined for: " + ", ".join(p["name"] for p in no_adr))
            for p in no_adr[:3]:
                notes.append(f"  {p['name']}: {p.get('adr_note') or 'unknown reason'}")
            notes.append("  -> in the port editor, 'a auto' infers them from the usual layout (please "
                         "verify), or set them with 'a <port> <adr>'")
        if inferred:
            notes.append("_ADR picked among several possible values / inferred (verify): "
                         + ", ".join(p["name"] for p in inferred))
            notes.append(f"  e.g. {inferred[0]['name']}: {inferred[0]['adr_note']}")
        if dyn:
            notes.append(f"{dyn} ports have a _UPC that firmware variables decide at runtime (e.g. UMAP/UMP3) - "
                         "the tables can't tell which are connected; pick them by testing "
                         "(devices in each port, IORegistryExplorer)")
        if unk:
            notes.append(f"{unk} ports use a _UPC form I can't evaluate - check keep/type manually")
        if c.get("bt_hint"):
            notes.append(f"the OEM table attaches its Bluetooth objects to {c['bt_hint']} (DBPN) - if your "
                         "Bluetooth is a USB device on this controller, map that port as 'internal'")
        if c.get("hub_has_sta"):
            notes.append(f"{c['hub']} already has an _STA method - see warning on generate")
        if notes:
            print(f"\n  Notes for {n}. {c['path']}.{c['hub']}:")
            for note in notes:
                print(f"   ! {note}")


def results_dir() -> Path:
    base = Path(__file__).resolve().parent / "Results"
    try:
        base.mkdir(exist_ok=True)
        return base
    except OSError:
        fallback = Path.cwd() / "Results"
        fallback.mkdir(exist_ok=True)
        return fallback


# --------------------------------------------------------------------------- interactive mode
@dataclass
class Session:
    iasl: Iasl
    acpi_label: str = ""
    controllers: list = field(default_factory=list)
    darwin: str = "osi"            # "osi" = _OSI ("Darwin"), "osdw" = OSDW ()


def darwin_label(mode: str) -> str:
    return "OSDW ()" if mode == "osdw" else '_OSI ("Darwin")'


def selected(c: dict) -> int:
    return sum(bool(p["keep"]) for p in c["ports"])


def parse_selection(spec: str, ports) -> list:
    """'1,2,5-8 HS03' -> sorted 1-based indices; port names are accepted too."""
    idx = set()
    for tok in re.split(r"[,\s]+", spec.strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            idx.update(range(min(a, b), max(a, b) + 1))
        elif tok.isdigit():
            idx.add(int(tok))
        else:
            hit = [i for i, p in enumerate(ports, 1) if p["name"].lower() == tok.lower()]
            if not hit:
                raise ValueError(f"unknown port '{tok}'")
            idx.add(hit[0])
    if not idx:
        raise ValueError("no ports given")
    bad = [i for i in idx if not 1 <= i <= len(ports)]
    if bad:
        raise ValueError(f"no such port number: {bad[0]}")
    return sorted(idx)


def select_acpi(sess: Session) -> bool:
    while True:
        head("Select ACPI Tables")
        if is_admin_windows():
            print(" NOTE: running as admin on Windows - drag and drop may not work.")
            print("       Shift + right-click the folder in Explorer, choose 'Copy as path'")
            print("       and paste it here instead.\n")
        print(" Drag and drop an ACPI folder (e.g. OpenCore's SysReport/ACPI)")
        print(" or single DSDT/SSDT files into this window, then press [enter].\n")
        print(" M. Main menu")
        print(" Q. Quit\n")
        raw = grab(" Path: ").strip()
        if not raw:
            continue
        if raw.lower() == "m":
            return False
        if raw.lower() == "q":
            raise SystemExit(0)
        paths = parse_dropped(raw)
        if not paths:
            print("\n That path does not exist.")
            grab(" Press [enter] to try again...")
            continue
        head("Loading ACPI Tables")
        try:
            sources = load_acpi(paths, sess.iasl)
            controllers = scan_sources(sources, "connectable")
        except UserError as e:
            print(f"\n{e}\n")
            grab(" Press [enter] to continue...")
            continue
        if not controllers:
            print("\nNo RHUB/HUBN device found in the DSDT. Make sure the DSDT of the system is included.\n")
            grab(" Press [enter] to continue...")
            continue
        sess.controllers = controllers
        where = str(paths[0]) if len(paths) == 1 else f"{len(paths)} dropped paths"
        sess.acpi_label = f"{where} ({len(sources)} table(s))"
        print()
        describe(controllers)
        grab("\n Press [enter] to choose ports...")
        return True


def edit_controller(c: dict) -> None:
    """Port editor for one controller."""
    ports = c["ports"]
    msg = ""
    while True:
        sel = selected(c)
        head("Choose Ports")
        print(f" {c['path']}   {c['hub']} -> {c['new_hub']}   (PCI {pci_label(c)})")
        if len(ports) > MAX_PORTS:
            print(f" {len(ports)} ports are declared, but macOS maps at most {MAX_PORTS} per controller -")
            print(" select the ones you actually need.")
        print(f" Selected: {sel}/{MAX_PORTS}" + ("   !! over the limit - deselect some" if sel > MAX_PORTS else ""))
        if c.get("hub_has_sta"):
            print(f" !! {c['hub']} already has an _STA method in the OEM table")
        print(f"\n  {'#':>3}  {'Port':<5} {'_ADR':<6} {'Use':<4} {'Type':<15} {'OEM':<4} Label")
        for n, p in enumerate(ports, 1):
            label = p.get("label", "")
            if p["name"] == c.get("bt_hint"):
                label = (label + "  " if label else "") + "<- OEM Bluetooth port (DBPN)"
            print(f"  {n:>3}  {p['name']:<5} {str(p.get('adr') or '?'):<6} [{'x' if p['keep'] else ' '}]  "
                  f"{str(p['type']):<15} {oem_state(p):<4} {label}")
        no_adr = [p for p in ports if not p.get("adr")]
        if no_adr:
            print(f"\n  No _ADR could be determined for {len(no_adr)} port(s) - use 'a auto' or 'a <ports> <adr>':")
            for p in no_adr[:3]:
                print(f"   ? {p['name']}: {p.get('adr_note') or 'unknown reason'}")
            if len(no_adr) > 3:
                print(f"   ... and {len(no_adr) - 3} more")
        inferred = [p for p in ports if str(p.get("adr_note", "")).startswith("inferred")]
        if inferred:
            print(f"\n  _ADR inferred (verify): {', '.join(p['name'] for p in inferred)}")
            print(f"   e.g. {inferred[0]['name']}: {inferred[0]['adr_note']}")
        print("\n  OEM column: on/off = literal in the table, dyn = decided by firmware at runtime, ? = unknown")
        print(f"  {INTERNAL_HINT}")
        print("\n  k <ports>        toggle        (e.g. k 1,2,5-8 or k HS01 SS01)")
        print("  on/off <ports>   select / deselect")
        print("  all | none | oem select everything / nothing / what the OEM table enables")
        print("  t <ports> <type> set type: typea usb3-a typec-hs typec-switch typec-noswitch internal | 0x0A")
        print("  a <ports> <adr>  set the _ADR of ports whose address wasn't found (e.g. a SS01 0x11)")
        print("  a auto           infer missing _ADRs from the usual layout (HSnn = nn, then USRn, then SSnn)")
        print("  l <port> <text>  set a label (comment in the SSDT)")
        print("  d. Done (back)   q. Quit")
        if msg:
            print(f"\n ! {msg}")
            msg = ""
        line = grab("\n Command: ").strip()
        if not line:
            continue
        parts = line.split(None, 1)
        verb, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        try:
            if verb in ("d", "done", "m"):
                return
            elif verb == "q":
                raise SystemExit(0)
            elif verb in ("k", "on", "off"):
                for idx in parse_selection(rest, ports):
                    p = ports[idx - 1]
                    p["keep"] = (not p["keep"]) if verb == "k" else (verb == "on")
            elif verb in ("all", "none", "oem"):
                for p in ports:
                    usr = p["name"].upper().startswith("USR") or not p.get("adr")
                    if verb == "all":
                        p["keep"] = not usr
                    elif verb == "none":
                        p["keep"] = False
                    else:
                        p["keep"] = oem_state(p) == "on" and not usr
            elif verb == "t":
                spec, _, tname = rest.rpartition(" ")
                v = parse_type(tname)
                for idx in parse_selection(spec, ports):
                    ports[idx - 1]["type"] = TYPE_NAMES.get(v, v)
            elif verb == "a":
                if rest.strip().lower() == "auto":
                    done = infer_adrs(ports)
                    msg = ("inferred _ADR for " + ", ".join(done) + " - please verify them (e.g. against "
                           "the DSDT or IORegistryExplorer)") if done else "nothing to infer"
                else:
                    spec, _, val = rest.rpartition(" ")
                    v = to_int(val)
                    for idx in parse_selection(spec, ports):
                        ports[idx - 1]["adr"] = hx(v)
                        ports[idx - 1]["adr_note"] = ""
            elif verb == "l":
                spec, _, text = rest.partition(" ")
                for idx in parse_selection(spec, ports):
                    ports[idx - 1]["label"] = text.strip()
            else:
                msg = f"unknown command '{verb}'"
        except ValueError as e:
            msg = str(e)


def edit_ports(sess: Session) -> None:
    cs = sess.controllers
    if not cs:
        print("\n Load ACPI tables first.")
        grab(" Press [enter] to return...")
        return
    if len(cs) == 1:
        edit_controller(cs[0])
        return
    msg = ""
    while True:
        head("Controllers")
        print(" Every USB controller has its own root hub. Only controllers with selected ports get a")
        print(" new hub - all others stay untouched.\n")
        print_controller_list(cs)
        print("\n  <number>    choose the ports of that controller")
        print("  only <n>    keep the selection of controller n, deselect all other controllers")
        print("  skip <n>    deselect all ports of controller n")
        print("  d. Done     q. Quit")
        if msg:
            print(f"\n ! {msg}")
            msg = ""
        line = grab("\n Command: ").strip().lower()
        if not line:
            continue
        if line in ("d", "done", "m"):
            return
        if line == "q":
            raise SystemExit(0)
        m = re.fullmatch(r"(only|skip)?\s*(\d+)", line)
        if not m or not 1 <= int(m.group(2)) <= len(cs):
            msg = f"enter a controller number between 1 and {len(cs)}, 'only <n>', 'skip <n>' or d"
            continue
        verb, n = m.group(1), int(m.group(2)) - 1
        if verb is None:
            edit_controller(cs[n])
        elif verb == "skip":
            for p in cs[n]["ports"]:
                p["keep"] = False
        else:
            for j, c in enumerate(cs):
                if j != n:
                    for p in c["ports"]:
                        p["keep"] = False


def generate_interactive(sess: Session) -> None:
    head("Generate SSDT-XHUB")
    if not sess.controllers:
        print(" Load ACPI tables (or a ports.json) first.")
        grab("\n Press [enter] to return...")
        return
    # macOS maps at most 15 ports per controller - ask which ones are needed
    for c in sess.controllers:
        while selected(c) > MAX_PORTS:
            print(f"\n {c['path']}.{c['hub']}: {selected(c)} ports selected, but macOS maps at most "
                  f"{MAX_PORTS} per controller.")
            grab(" Press [enter] to choose which ports you need...")
            edit_controller(c)
            head("Generate SSDT-XHUB")
    dsl = results_dir() / "SSDT-XHUB.dsl"
    ctrls = build_ssdt({"controllers": sess.controllers}, dsl, darwin=sess.darwin)
    if ctrls is None:
        grab("\n Press [enter] to return...")
        return
    aml = compile_dsl(dsl, sess.iasl)
    if aml:
        print(f"Compiled {aml}")
        print("\n Drag and drop your EFI/OC folder here to copy the SSDT and enable it in")
        print(" config.plist (a backup is made), or just press [enter] to skip.")
        raw = grab(" EFI/OC folder: ").strip()
        if raw:
            target = check_path(raw)
            if target and target.is_dir():
                try:
                    install_to_efi(aml, target)
                except UserError as e:
                    print(f"\n ! {e}")
            else:
                print(" Not a folder - skipped.")
    print("\n Next: reboot, then check in IORegistryExplorer that the new hub is active")
    print(" and the OEM hub is gone under macOS.")
    grab("\n Press [enter] to return...")


def save_json_interactive(sess: Session) -> None:
    if not sess.controllers:
        print("\n Nothing to save yet.")
    else:
        path = results_dir() / "ports.json"
        write_ports_json(path, sess.controllers)
        print(f"\n Saved {path}")
    grab(" Press [enter] to return...")


def load_json_interactive(sess: Session) -> None:
    head("Load ports.json")
    raw = grab(" Drag and drop a ports.json file here: ").strip()
    p = check_path(raw) if raw else None
    if not p or not p.is_file():
        print("\n Not a file.")
    else:
        try:
            sess.controllers = read_ports_json(p)["controllers"]
            sess.acpi_label = f"{p.name} (ports.json)"
            print("\n Loaded.")
        except UserError as e:
            print(f"\n ! {e}")
    grab(" Press [enter] to return...")


def interactive(iasl_path=None, auto_download=False) -> None:
    sess = Session(iasl=Iasl(iasl_path, auto_download))
    while True:
        head("XHUB Mapper")
        print(f" ACPI:   {sess.acpi_label or 'not loaded'}")
        if sess.controllers:
            keep = sum(selected(c) for c in sess.controllers)
            total = sum(len(c["ports"]) for c in sess.controllers)
            print(f" Hubs:   {len(sess.controllers)} controller(s), {keep}/{total} ports selected")
        print(f" iasl:   {sess.iasl.peek() or 'not found - you will be offered a download when it is needed'}")
        print(f" Darwin check in _STA: {darwin_label(sess.darwin)}\n")
        print(" 1. Select ACPI folder / tables  (drag and drop)")
        print(" 2. Choose controllers / ports")
        print(" 3. Generate SSDT-XHUB")
        print(" 4. Save ports.json")
        print(" 5. Load ports.json")
        print(" 6. Toggle Darwin check (_OSI (\"Darwin\") <-> OSDW ())")
        print(" Q. Quit\n")
        choice = grab(" Please select an option: ").strip().lower()
        if choice == "1":
            if select_acpi(sess):
                edit_ports(sess)
        elif choice == "2":
            edit_ports(sess)
        elif choice == "3":
            generate_interactive(sess)
        elif choice == "4":
            save_json_interactive(sess)
        elif choice == "5":
            load_json_interactive(sess)
        elif choice == "6":
            sess.darwin = "osdw" if sess.darwin == "osi" else "osi"
        elif choice == "q":
            raise SystemExit(0)


# --------------------------------------------------------------------------- CLI
def ask_for_paths():
    """Prompt for a drag & dropped path when 'scan' was started without arguments."""
    print("Drag and drop an ACPI folder (e.g. SysReport/ACPI) or DSDT/SSDT files here,")
    print("then press [enter] (empty input aborts).\n")
    while True:
        raw = grab("Path: ").strip()
        if not raw:
            raise UserError("no path given")
        paths = parse_dropped(raw)
        if paths:
            return paths
        print("That path does not exist - try again.")


def cmd_scan(a) -> None:
    iasl = Iasl(a.iasl, a.download_iasl)
    paths = [Path(p).expanduser() for p in a.paths] or ask_for_paths()
    sources = load_acpi(paths, iasl)
    controllers = scan_sources(sources, a.preselect)
    if not controllers:
        raise UserError("No RHUB/HUBN device found in the DSDT. Make sure the DSDT of the system is part "
                        "of the input.")
    write_ports_json(Path(a.output), controllers)
    describe(controllers)
    print(f"\nWrote {a.output} - edit it, then run: {Path(sys.argv[0]).name} generate {a.output}")


def cmd_generate(a) -> None:
    cfg = read_ports_json(Path(a.config))
    dsl = Path(a.output)
    if build_ssdt(cfg, dsl, a.oem_id, a.table_id, a.emit_disabled, a.darwin_check) is None:
        sys.exit(1)
    if a.compile or a.efi:
        aml = compile_dsl(dsl, Iasl(a.iasl, a.download_iasl))
        if aml:
            print(f"Compiled {aml}")
            if a.efi:
                install_to_efi(aml, Path(a.efi).expanduser())
        elif a.efi:
            raise UserError("Skipping EFI install because compilation failed.")
    print("\nNext: reboot, then verify in IORegistryExplorer that the new hub is active "
          "and the OEM hub is gone under macOS.")


def cmd_interactive(a) -> None:
    interactive(a.iasl, a.download_iasl)


def add_iasl_args(p) -> None:
    p.add_argument("--iasl", help="path to your own iasl")
    p.add_argument("--download-iasl", action="store_true",
                   help="download iasl into ./tools without asking if no working one is found")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Automate the XHUB/HUBX ACPI USB mapping method. "
                    "Run without arguments for the interactive drag-and-drop mode.")
    sub = ap.add_subparsers(dest="cmd")

    i = sub.add_parser("interactive", help="menu-driven mode with drag & drop (default)")
    add_iasl_args(i)
    i.set_defaults(func=cmd_interactive)

    s = sub.add_parser("scan", help="find USB hubs/ports in DSDT/SSDT and write ports.json")
    s.add_argument("paths", nargs="*",
                   help=".dsl/.aml files or folders (e.g. SysReport/ACPI); none = drag & drop prompt")
    s.add_argument("-o", "--output", default="ports.json")
    s.add_argument("--preselect", choices=["connectable", "all", "none"], default="connectable",
                   help="which ports get keep=true initially (default: connectable per OEM _UPC)")
    add_iasl_args(s)
    s.set_defaults(func=cmd_scan)

    g = sub.add_parser("generate", help="render (and optionally compile/install) SSDT-XHUB")
    g.add_argument("config", help="ports.json created by 'scan' or the interactive mode")
    g.add_argument("-o", "--output", default="SSDT-XHUB.dsl")
    g.add_argument("--compile", action="store_true", help="compile with iasl")
    g.add_argument("--efi", help="path to EFI/OC (or EFI): copy .aml and enable it in config.plist")
    g.add_argument("--emit-disabled", action="store_true",
                   help="also emit non-selected ports with _UPC connectable = 0")
    g.add_argument("--darwin-check", choices=["osi", "osdw"], default="osi",
                   help='condition used in the _STA methods: _OSI ("Darwin") (default, no dependencies) '
                        "or OSDW () (needs SSDT-OSDW or a native OSDW method)")
    g.add_argument("--oem-id", default="OCLT")
    g.add_argument("--table-id", default="XHUB")
    add_iasl_args(g)
    g.set_defaults(func=cmd_generate)

    args = ap.parse_args()
    try:
        if args.cmd is None:
            interactive()
        else:
            args.func(args)
    except UserError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
