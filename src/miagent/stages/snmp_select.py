"""SNMP object selection — the only LLM call in the SNMP path.

Everything else (MIB parsing, snmp.yml compilation, IR derivation) is
deterministic, so this stage's whole job is judgment: which of the hundreds
of objects in a MIB are worth monitoring, what to call them in Prometheus
terms, and which column makes a row human-identifiable.
"""

from __future__ import annotations

from miagent.llm.router import LLMRouter, Stage
from miagent.snmp.mib import MibInventory
from miagent.snmp.models import SnmpSelection
from miagent.validate.report import ValidationReport

SYSTEM = """\
You select SNMP objects to monitor and name them for Prometheus.

Selection rules:
- Pick objects that describe health, throughput, errors, saturation, or
  capacity. Skip configuration/inventory objects that never change, and
  skip administrative objects (row-status, test/control columns).
- Prefer 64-bit High Capacity counters (e.g. ifHCInOctets) over their
  32-bit equivalents (ifInOctets) when both exist — 32-bit counters wrap.
- 10-25 metrics is a good target for a device MIB. Fewer, well-chosen
  metrics beat exhaustive coverage.

Naming rules (Prometheus conventions):
- snake_case, prefixed with the subsystem (e.g. network_interface_),
  never the raw MIB camelCase name.
- Cumulative counters end in _total. Gauges do not.
- Include the unit as a suffix: _bytes, _seconds, _packets, _errors_total.
- If the object's description says the value is a rate ("per second",
  "bits per second"), name the unit as a rate: _bits_per_second,
  _packets_per_second — not the bare quantity.
- Time objects are handled for you: the compiler scales SNMP time types
  into seconds and fixes the suffix, so don't invent tick/centisecond
  names for them.
- Do not put values in metric names — those belong in labels.

Lookups:
- For every table you select metrics from, add ONE lookup that turns the
  numeric index into a readable label (e.g. index ifIndex -> source
  ifDescr -> label `interface`). Use a short snake_case label name.
- Only use a source object that exists in the same table as the index.
- index_labels must exactly match the table's INDEX column names."""


def select_snmp_objects(
    router: LLMRouter,
    inv: MibInventory,
    service: str,
    hint: str = "",
) -> SnmpSelection:
    prompt = (
        f"Device/service: {service}\n"
        + (f"Operator guidance: {hint}\n" if hint else "")
        + "\nAvailable MIB objects (name, OID, SNMP syntax, mapped type, "
        "enum values, description):\n"
        f"{inv.condense()}\n\n"
        "Select the objects worth monitoring, name them for Prometheus, and "
        "define one index lookup per table you use."
    )
    return router.structured(Stage.snmp_select, prompt, SnmpSelection, system=SYSTEM)


def repair_snmp_selection(
    router: LLMRouter,
    inv: MibInventory,
    selection: SnmpSelection,
    report: ValidationReport,
    walk_sample: str,
    iteration: int,
    compile_error: str = "",
) -> SnmpSelection:
    """Repair the *selection*, not the YAML.

    snmp.yml is compiled deterministically, so a validation failure means
    the selection was wrong — an object the device doesn't implement, a
    lookup whose source column is empty, a type that doesn't match reality.
    Feeding back an actual SNMP walk of the device is what makes this
    fixable: it shows which OIDs genuinely respond.
    """
    problem = (
        f"The compiled config failed to build:\n{compile_error}\n"
        if compile_error
        else f"Validation report:\n{report.summary()}\n"
    )
    prompt = (
        f"Repair iteration {iteration}. The previous SNMP selection did not "
        "validate against the real device.\n\n"
        + problem
        + "\nLive SNMP walk of the device (ground truth — only OIDs listed "
        "here actually respond; an object absent here is not implemented "
        "and must be dropped from the selection):\n"
        f"<<<WALK\n{walk_sample}\nWALK>>>\n\n"
        "Previous selection:\n"
        f"{selection.model_dump_json(indent=2)}\n\n"
        "Available MIB objects:\n"
        f"{inv.condense(max_objects=200)}\n\n"
        "Return a corrected selection:\n"
        "1. Keep every metric that already worked, unchanged.\n"
        "2. For each failed metric, look in the walk for an equivalent the "
        "device DOES implement and substitute it — e.g. if a 64-bit "
        "ifHC* counter is absent but its 32-bit counterpart has values, "
        "use the 32-bit one; if ifHighSpeed is absent, use ifSpeed. Only "
        "drop a metric when the walk shows no working equivalent.\n"
        "3. Repoint any lookup whose source column returned no value at a "
        "column that did (e.g. ifName -> ifDescr).\n"
        "Do not give up coverage you can recover: an OID with values in "
        "the walk is usable even if the object you originally chose is not."
    )
    return router.structured(
        Stage.snmp_select,
        prompt,
        SnmpSelection,
        system=SYSTEM,
        repair_iteration=iteration,
    )
