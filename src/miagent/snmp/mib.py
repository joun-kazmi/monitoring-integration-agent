"""MIB parsing → OID inventory. Fully deterministic (pysmi, no LLM).

`compile_mibs` drives pysmi to turn MIB modules (fetched by name or read
from local files) into JSON, then `load_mib_json` flattens that into a
`MibInventory`: every readable object with its OID, SNMP syntax, table
membership, index columns, and enum values.

That inventory is what the selection stage sees (condensed) and what the
snmp.yml compiler consumes (in full).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_MIB_SOURCE = "https://mibs.pysnmp.com/asn1/"

# SNMP syntax -> snmp_exporter metric type.
# https://github.com/prometheus/snmp_exporter#metric-types
_TYPE_MAP = {
    "Counter32": "counter",
    "Counter64": "counter",
    "Gauge32": "gauge",
    "Integer32": "gauge",
    "INTEGER": "gauge",
    "Unsigned32": "gauge",
    "TimeTicks": "gauge",
    "TimeInterval": "gauge",
    "TimeStamp": "gauge",
    "InterfaceIndex": "gauge",
    "DisplayString": "DisplayString",
    "SnmpAdminString": "DisplayString",
    "OCTET STRING": "OctetString",
    "IpAddress": "IpAddr",
    "InetAddress": "InetAddress",
    "InetAddressIPv4": "InetAddress",
    "InetAddressIPv6": "InetAddress",
    "PhysAddress": "PhysAddress48",
    "MacAddress": "PhysAddress48",
    "OBJECT IDENTIFIER": "OctetString",
    "Opaque": "OctetString",
    "Bits": "gauge",
    "Counter": "counter",
    "Gauge": "gauge",
    "TruthValue": "gauge",
}

# Types usable as a label value (lookups / string indexes).
_LABEL_TYPES = {"DisplayString", "OctetString", "IpAddr", "InetAddress", "PhysAddress48"}

_NUMERIC_TYPES = {"counter", "gauge"}


def snmp_type_of(syntax_type: str) -> str:
    """Map a MIB syntax type name to an snmp_exporter type."""
    return _TYPE_MAP.get(syntax_type, "gauge")


@dataclass
class MibObject:
    name: str
    oid: str
    module: str
    nodetype: str  # scalar | column | table | row | notification | ...
    syntax: str = ""  # raw MIB syntax type, e.g. "Counter32"
    snmp_type: str = "gauge"  # mapped snmp_exporter type
    description: str = ""
    maxaccess: str = ""
    enum_values: dict[str, str] = field(default_factory=dict)  # {"1": "up", ...}
    table: str = ""  # parent table name for columns
    table_oid: str = ""
    index_names: list[str] = field(default_factory=list)  # table INDEX columns

    @property
    def is_numeric(self) -> bool:
        return self.snmp_type in _NUMERIC_TYPES

    @property
    def is_label_like(self) -> bool:
        return self.snmp_type in _LABEL_TYPES

    @property
    def readable(self) -> bool:
        return self.maxaccess in ("read-only", "read-write", "read-create", "accessible-for-notify")


@dataclass
class MibInventory:
    objects: dict[str, MibObject] = field(default_factory=dict)

    def get(self, name: str) -> Optional[MibObject]:
        return self.objects.get(name)

    def metric_candidates(self) -> list[MibObject]:
        """Readable numeric scalars and columns — the things worth scraping."""
        return [
            o
            for o in self.objects.values()
            if o.readable and o.is_numeric and o.nodetype in ("scalar", "column")
        ]

    def label_candidates(self, table: str) -> list[MibObject]:
        """String-ish columns in a table, usable as lookup labels."""
        return [
            o
            for o in self.objects.values()
            if o.table == table and o.readable and o.is_label_like
        ]

    def tables(self) -> dict[str, list[MibObject]]:
        out: dict[str, list[MibObject]] = {}
        for o in self.objects.values():
            if o.nodetype == "column":
                out.setdefault(o.table, []).append(o)
        return out

    def condense(self, max_objects: int = 400) -> str:
        """Compact text inventory for the selection prompt."""
        lines: list[str] = []
        tables = self.tables()
        scalars = [
            o for o in self.objects.values()
            if o.nodetype == "scalar" and o.readable and (o.is_numeric or o.is_label_like)
        ]
        budget = max_objects

        if scalars:
            lines.append("## Scalar objects")
            for o in sorted(scalars, key=lambda x: x.oid)[:budget]:
                budget -= 1
                desc = o.description.replace("\n", " ")[:140]
                lines.append(
                    f"- {o.name} oid={o.oid} syntax={o.syntax} type={o.snmp_type}"
                    + (f" enum={sorted(o.enum_values.items())}" if o.enum_values else "")
                    + (f" — {desc}" if desc else "")
                )

        for tname, cols in sorted(tables.items()):
            if budget <= 0:
                lines.append("\n[... inventory truncated by budget ...]")
                break
            idx = cols[0].index_names if cols else []
            lines.append(f"\n## Table {tname} (INDEX: {', '.join(idx) or 'unknown'})")
            for o in sorted(cols, key=lambda x: x.oid):
                if budget <= 0:
                    break
                if not o.readable or not (o.is_numeric or o.is_label_like):
                    continue
                budget -= 1
                desc = o.description.replace("\n", " ")[:140]
                lines.append(
                    f"- {o.name} oid={o.oid} syntax={o.syntax} type={o.snmp_type}"
                    + (f" enum={sorted(o.enum_values.items())}" if o.enum_values else "")
                    + (f" — {desc}" if desc else "")
                )
        return "\n".join(lines)


def _mibdump_binary() -> str:
    for cand in ("mibdump", str(Path.home() / ".local/bin/mibdump"), "mibdump.py"):
        if shutil.which(cand) or Path(cand).exists():
            return cand
    raise RuntimeError("mibdump (pysmi) not found — pip install --user pysmi")


def compile_mibs(
    modules: list[str],
    outdir: Path,
    mib_sources: Optional[list[str]] = None,
    rebuild: bool = False,
) -> list[Path]:
    """Compile MIB modules to JSON with pysmi. Returns the JSON paths.

    `modules` are MIB module names (e.g. "IF-MIB"). `mib_sources` may mix
    URLs and local directories; local dirs let you compile vendor MIB files
    that aren't published anywhere.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    sources = mib_sources or [DEFAULT_MIB_SOURCE]
    cmd = [
        _mibdump_binary(),
        "--destination-format", "json",
        "--destination-directory", str(outdir),
        "--generate-mib-texts",  # keep DESCRIPTIONs: used for help strings
        "--quiet",
    ]
    for src in sources:
        cmd += ["--mib-source", src]
    if rebuild:
        cmd.append("--rebuild")
    cmd += modules

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(
            f"mibdump failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:1500]}"
        )
    paths = [outdir / f"{m}.json" for m in modules]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise RuntimeError(
            f"mibdump produced no JSON for: {missing}. "
            f"Output: {(proc.stdout + proc.stderr).strip()[:1000]}"
        )
    return paths


def load_mib_json(paths: list[Path]) -> MibInventory:
    """Flatten pysmi JSON into a MibInventory."""
    inv = MibInventory()
    raw: dict[str, dict] = {}
    module_of: dict[str, str] = {}

    for path in paths:
        data = json.loads(path.read_text())
        module = path.stem
        for name, node in data.items():
            if not isinstance(node, dict) or node.get("class") != "objecttype":
                continue
            raw[name] = node
            module_of[name] = module

    # Row nodes carry the INDEX clause and define table membership by OID prefix.
    rows = {n: v for n, v in raw.items() if v.get("nodetype") == "row"}
    tables = {n: v for n, v in raw.items() if v.get("nodetype") == "table"}

    def row_indices(row_name: str, _seen: Optional[set] = None) -> list[str]:
        """INDEX columns for a row, following AUGMENTS chains.

        A row declared `AUGMENTS ifEntry` (e.g. IF-MIB's ifXEntry, which
        holds the 64-bit counters) has no INDEX of its own and inherits
        the base row's — miss this and every ifX metric loses its labels.
        """
        _seen = _seen or set()
        if row_name in _seen:
            return []
        _seen.add(row_name)
        node = rows.get(row_name) or {}
        idx = [i.get("object", "") for i in (node.get("indices") or []) if i.get("object")]
        if idx:
            return idx
        aug = node.get("augmention") or {}
        base = aug.get("object")
        if base:
            base_row = base if base in rows else None
            if base_row is None:
                # `object` may name the base row directly or via its module.
                base_row = next((r for r in rows if r == base), None)
            if base_row:
                return row_indices(base_row, _seen)
        return []

    def owning_row(oid: str) -> Optional[tuple[str, dict]]:
        best: Optional[tuple[str, dict]] = None
        for rname, rnode in rows.items():
            roid = rnode.get("oid", "")
            if roid and oid.startswith(roid + "."):
                if best is None or len(roid) > len(best[1].get("oid", "")):
                    best = (rname, rnode)
        return best

    for name, node in raw.items():
        nodetype = node.get("nodetype", "")
        if nodetype not in ("scalar", "column", "table", "row"):
            continue
        oid = node.get("oid", "")
        syntax = (node.get("syntax") or {}).get("type", "")
        constraints = (node.get("syntax") or {}).get("constraints") or {}
        enumeration = constraints.get("enumeration") or {}
        # pysmi gives {label: value}; snmp.yml wants {value: label}
        enum_values = {str(v): k for k, v in enumeration.items()}

        obj = MibObject(
            name=name,
            oid=oid,
            module=module_of.get(name, ""),
            nodetype=nodetype,
            syntax=syntax,
            snmp_type=snmp_type_of(syntax),
            description=(node.get("description") or "").strip(),
            maxaccess=node.get("maxaccess", ""),
            enum_values=enum_values,
        )

        if nodetype == "column" and oid:
            found = owning_row(oid)
            if found:
                rname, rnode = found
                # Table name: the row's parent table node, else derive from row name.
                tname = next(
                    (
                        tn
                        for tn, tv in tables.items()
                        if rnode.get("oid", "").startswith(tv.get("oid", "") + ".")
                    ),
                    rname,
                )
                obj.table = tname
                obj.table_oid = tables.get(tname, {}).get("oid", "")
                obj.index_names = row_indices(rname)
        inv.objects[name] = obj

    return inv
