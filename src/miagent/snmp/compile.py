"""Compile a selection + MIB inventory into artifacts. No LLM.

Three outputs, all derived from the same inputs so they cannot drift:

- `snmp.yml`      — what snmp_exporter actually runs (we compile this
                    ourselves; it is the subset of the upstream generator's
                    job that our selection needs)
- `generator.yml` — the human-maintainable source of truth, for users who
                    want to re-run the official generator in CI
- `IntegrationSpec` (IR) — the validation contract, so expectations are
                    generated from the same selection as the artifact
"""

from __future__ import annotations

from typing import Any, Optional

import yaml

from miagent.ir import (
    EndpointSpec,
    IntegrationSpec,
    LabelSpec,
    MetricSpec,
    MetricType,
    ValueExpectation,
)
from miagent.snmp.mib import MibInventory, MibObject
from miagent.snmp.models import SnmpSelection

DEFAULT_AUTH = "public_v2"

# SNMP time types are expressed in hundredths of a second. snmp_exporter's
# own generator converts them with `scale: 0.01` and a `_seconds` name; we
# apply the same rule in code so unit correctness never depends on the
# model naming things right.
_CENTISECOND_TYPES = {"TimeTicks", "TimeInterval", "TimeStamp"}
_CENTISECOND_SCALE = 0.01
# Unit suffixes a model may attach to a time metric that are wrong once the
# value has been scaled into seconds. Stripped longest-first.
_WRONG_TIME_SUFFIXES = (
    "_timeticks", "_centiseconds", "_hundredths", "_milliseconds",
    "_ticks", "_total", "_msec", "_ms", "_cs",
)


class CompileError(ValueError):
    """Selection references something the MIB inventory doesn't support."""


def _index_type(inv: MibInventory, index_name: str) -> str:
    obj = inv.get(index_name)
    if obj is None:
        return "gauge"
    return obj.snmp_type if obj.snmp_type != "counter" else "gauge"


def normalize_metric_name(name: str, snmp_type: str, syntax: str = "") -> str:
    """Enforce Prometheus unit/suffix conventions deterministically.

    - counters end in `_total`, gauges do not
    - centisecond types (TimeTicks & friends) end in `_seconds`, since
      `compile_snmp_yaml` scales their values into seconds
    """
    name = name.strip()
    if syntax in _CENTISECOND_TYPES:
        # Longest suffix first, so `_timeticks` wins over `_ticks`.
        for wrong in sorted(_WRONG_TIME_SUFFIXES, key=len, reverse=True):
            if name.endswith(wrong):
                name = name[: -len(wrong)]
                break
        name = name.rstrip("_")
        return name if name.endswith("_seconds") else name + "_seconds"
    if snmp_type == "counter" and not name.endswith("_total"):
        return name + "_total"
    if snmp_type != "counter" and name.endswith("_total"):
        return name[: -len("_total")]
    return name


def _resolve(inv: MibInventory, selection: SnmpSelection) -> list[tuple[MibObject, Any]]:
    """Pair each selected metric with its MIB object, validating as we go."""
    resolved = []
    for sel in selection.metrics:
        obj = inv.get(sel.mib_object)
        if obj is None:
            raise CompileError(f"unknown MIB object {sel.mib_object!r}")
        if not obj.oid:
            raise CompileError(f"{sel.mib_object!r} has no OID")
        if obj.nodetype not in ("scalar", "column"):
            raise CompileError(
                f"{sel.mib_object!r} is a {obj.nodetype}, not a scalar/column"
            )
        if not obj.is_numeric:
            raise CompileError(
                f"{sel.mib_object!r} has non-numeric type {obj.snmp_type!r}"
            )
        resolved.append((obj, sel))
    if not resolved:
        raise CompileError("selection contains no usable metrics")
    return resolved


def _lookups_for(
    inv: MibInventory, selection: SnmpSelection, obj: MibObject
) -> list[dict]:
    """snmp.yml lookup entries applicable to one metric's index set."""
    if not obj.index_names:
        return []
    out = []
    for lk in selection.lookups:
        if not set(lk.index_labels).issubset(set(obj.index_names)):
            continue
        src = inv.get(lk.source_object)
        if src is None or not src.oid:
            continue
        out.append(
            {
                "labels": list(lk.index_labels),
                "labelname": lk.label_name,
                "oid": src.oid,
                "type": src.snmp_type,
            }
        )
    return out


def compile_snmp_yaml(
    inv: MibInventory,
    selection: SnmpSelection,
    community: str = "public",
    version: int = 2,
) -> str:
    """Produce snmp.yml — the config snmp_exporter runs."""
    resolved = _resolve(inv, selection)

    walk: list[str] = []
    metrics: list[dict] = []

    for obj, sel in resolved:
        name = normalize_metric_name(sel.metric_name, obj.snmp_type, obj.syntax)
        entry: dict[str, Any] = {
            "name": name,
            "oid": obj.oid,
            "type": obj.snmp_type,
            "help": (sel.help or obj.description.split(".")[0] or name).strip()
            + f" - {obj.name} ({obj.oid})",
        }
        if obj.syntax in _CENTISECOND_TYPES:
            entry["scale"] = _CENTISECOND_SCALE
        if obj.index_names:
            entry["indexes"] = [
                {"labelname": idx, "type": _index_type(inv, idx)}
                for idx in obj.index_names
            ]
            lookups = _lookups_for(inv, selection, obj)
            if lookups:
                entry["lookups"] = lookups
        if obj.enum_values:
            # snmp_exporter unmarshals these keys into Go ints — string keys
            # are rejected at config load ("cannot unmarshal !!str into int").
            entry["enum_values"] = {
                int(k): v for k, v in sorted(obj.enum_values.items(), key=lambda kv: int(kv[0]))
            }
        metrics.append(entry)
        walk.append(obj.oid)

    # Lookup source OIDs must also be walked or the labels come back empty.
    for lk in selection.lookups:
        src = inv.get(lk.source_object)
        if src and src.oid:
            walk.append(src.oid)

    doc = {
        "auths": {
            DEFAULT_AUTH: {
                "community": community,
                "security_level": "noAuthNoPriv",
                "auth_protocol": "MD5",
                "priv_protocol": "DES",
                "version": version,
            }
        },
        "modules": {
            selection.module_name: {
                "walk": sorted(set(walk), key=lambda o: [int(p) for p in o.split(".")]),
                "metrics": metrics,
            }
        },
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=1000)


def compile_generator_yaml(inv: MibInventory, selection: SnmpSelection) -> str:
    """Produce generator.yml — the human-maintained source of truth.

    Feeding this to the upstream snmp_exporter generator reproduces an
    equivalent snmp.yml (its output will carry the MIB object names in
    `help` and may include extra sibling columns).
    """
    resolved = _resolve(inv, selection)
    modules_mibs = sorted({obj.module for obj, _ in resolved if obj.module})

    walk = sorted(
        {obj.name for obj, _ in resolved}
        | {lk.source_object for lk in selection.lookups}
    )
    lookups = [
        {
            "source_indexes": list(lk.index_labels),
            "lookup": lk.source_object,
            "drop_source_indexes": False,
        }
        for lk in selection.lookups
    ]
    overrides = {}
    for obj, sel in resolved:
        name = normalize_metric_name(sel.metric_name, obj.snmp_type, obj.syntax)
        if name != obj.name:
            # The upstream generator has no rename field; record the intended
            # Prometheus name so the mapping isn't lost for human maintainers.
            overrides[obj.name] = {"rename_to": name}

    doc: dict[str, Any] = {
        "auths": {
            DEFAULT_AUTH: {"community": "public", "version": 2},
        },
        "modules": {
            selection.module_name: {
                "walk": walk,
                **({"lookups": lookups} if lookups else {}),
                **({"overrides": overrides} if overrides else {}),
            }
        },
    }
    header = (
        "# snmp_exporter generator config, produced by miagent.\n"
        f"# MIB modules required: {', '.join(modules_mibs) or 'n/a'}\n"
        "# Run: generator generate -m <mib-dir> -g generator.yml -o snmp.yml\n"
        "# NOTE: `rename_to` under overrides is a miagent annotation recording the\n"
        "# intended Prometheus metric name; the upstream generator ignores it.\n"
    )
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=1000)


def selection_to_ir(
    inv: MibInventory,
    selection: SnmpSelection,
    service: str,
    target: str = "",
    module: Optional[str] = None,
) -> IntegrationSpec:
    """Derive the validation contract from the same selection.

    No LLM call: the IR is a mechanical consequence of the selection, which
    is what keeps artifact and expectations from drifting.
    """
    resolved = _resolve(inv, selection)
    module = module or selection.module_name

    metrics: list[MetricSpec] = []
    for obj, sel in resolved:
        name = normalize_metric_name(sel.metric_name, obj.snmp_type, obj.syntax)
        labels: list[LabelSpec] = []
        if obj.index_names:
            for idx in obj.index_names:
                labels.append(LabelSpec(name=idx, description=f"table index {idx}"))
            for lk in selection.lookups:
                if set(lk.index_labels).issubset(set(obj.index_names)):
                    labels.append(
                        LabelSpec(
                            name=lk.label_name,
                            description=f"lookup from {lk.source_object}",
                        )
                    )
        value = ValueExpectation(min=0) if obj.snmp_type == "counter" else ValueExpectation()
        metrics.append(
            MetricSpec(
                name=name,
                type=MetricType.counter if obj.snmp_type == "counter" else MetricType.gauge,
                help=sel.help or obj.description.split(".")[0],
                labels=labels,
                value=value,
                source=f"SNMP {obj.name} oid={obj.oid} module={obj.module}",
            )
        )

    return IntegrationSpec(
        service=service,
        kind="snmp_generator",
        endpoints=[
            EndpointSpec(
                url=target or "snmp://target",
                response_format="snmp",
                notes=f"snmp_exporter module={module}, auth={DEFAULT_AUTH}",
            )
        ],
        metrics=metrics,
        notes=selection.notes,
    )
