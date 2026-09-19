import json
from pathlib import Path

import pytest
import yaml

from miagent.ir import MetricType
from miagent.snmp.compile import (
    CompileError,
    compile_generator_yaml,
    compile_snmp_yaml,
    normalize_metric_name,
    selection_to_ir,
)
from miagent.snmp.mib import load_mib_json, snmp_type_of
from miagent.snmp.models import SnmpLookup, SnmpMetric, SnmpSelection

# Minimal pysmi-shaped JSON: a scalar, a table with an INDEX, an enum column,
# and an AUGMENTS table (the ifXTable pattern that inherits its index).
MIB_JSON = {
    "devNumber": {
        "class": "objecttype", "name": "devNumber", "nodetype": "scalar",
        "oid": "1.3.6.1.4.1.9999.1", "maxaccess": "read-only",
        "description": "Number of widgets.",
        "syntax": {"class": "type", "type": "Integer32"},
    },
    "devTable": {
        "class": "objecttype", "name": "devTable", "nodetype": "table",
        "oid": "1.3.6.1.4.1.9999.2", "maxaccess": "not-accessible",
    },
    "devEntry": {
        "class": "objecttype", "name": "devEntry", "nodetype": "row",
        "oid": "1.3.6.1.4.1.9999.2.1", "maxaccess": "not-accessible",
        "indices": [{"implied": 0, "module": "DEV-MIB", "object": "devIndex"}],
    },
    "devIndex": {
        "class": "objecttype", "name": "devIndex", "nodetype": "column",
        "oid": "1.3.6.1.4.1.9999.2.1.1", "maxaccess": "read-only",
        "syntax": {"class": "type", "type": "Integer32"},
    },
    "devName": {
        "class": "objecttype", "name": "devName", "nodetype": "column",
        "oid": "1.3.6.1.4.1.9999.2.1.2", "maxaccess": "read-only",
        "syntax": {"class": "type", "type": "DisplayString"},
    },
    "devBytesIn": {
        "class": "objecttype", "name": "devBytesIn", "nodetype": "column",
        "oid": "1.3.6.1.4.1.9999.2.1.3", "maxaccess": "read-only",
        "description": "Octets received.",
        "syntax": {"class": "type", "type": "Counter32"},
    },
    "devState": {
        "class": "objecttype", "name": "devState", "nodetype": "column",
        "oid": "1.3.6.1.4.1.9999.2.1.4", "maxaccess": "read-only",
        "syntax": {"class": "type", "type": "INTEGER",
                   "constraints": {"enumeration": {"up": 1, "down": 2}}},
    },
    "devXTable": {
        "class": "objecttype", "name": "devXTable", "nodetype": "table",
        "oid": "1.3.6.1.4.1.9999.3", "maxaccess": "not-accessible",
    },
    "devXEntry": {
        "class": "objecttype", "name": "devXEntry", "nodetype": "row",
        "oid": "1.3.6.1.4.1.9999.3.1", "maxaccess": "not-accessible",
        "augmention": {"module": "DEV-MIB", "name": "devXEntry", "object": "devEntry"},
    },
    "devHCBytesIn": {
        "class": "objecttype", "name": "devHCBytesIn", "nodetype": "column",
        "oid": "1.3.6.1.4.1.9999.3.1.1", "maxaccess": "read-only",
        "syntax": {"class": "type", "type": "Counter64"},
    },
    "devUptime": {
        "class": "objecttype", "name": "devUptime", "nodetype": "scalar",
        "oid": "1.3.6.1.4.1.9999.4", "maxaccess": "read-only",
        "description": "Time since boot.",
        "syntax": {"class": "type", "type": "TimeTicks"},
    },
    "devSecret": {  # not readable: must never be selectable
        "class": "objecttype", "name": "devSecret", "nodetype": "column",
        "oid": "1.3.6.1.4.1.9999.2.1.9", "maxaccess": "not-accessible",
        "syntax": {"class": "type", "type": "Counter32"},
    },
}


@pytest.fixture
def inv(tmp_path):
    p = tmp_path / "DEV-MIB.json"
    p.write_text(json.dumps(MIB_JSON))
    return load_mib_json([p])


@pytest.fixture
def selection():
    return SnmpSelection(
        module_name="dev_mib",
        metrics=[
            SnmpMetric(mib_object="devNumber", metric_name="widgets", help="Widget count"),
            SnmpMetric(mib_object="devBytesIn", metric_name="widget_receive_bytes",
                       help="Bytes in"),
            SnmpMetric(mib_object="devHCBytesIn", metric_name="widget_receive_bytes_hc_total"),
            SnmpMetric(mib_object="devState", metric_name="widget_state"),
        ],
        lookups=[SnmpLookup(index_labels=["devIndex"], source_object="devName",
                            label_name="widget")],
    )


def test_type_mapping():
    assert snmp_type_of("Counter32") == "counter"
    assert snmp_type_of("Counter64") == "counter"
    assert snmp_type_of("Gauge32") == "gauge"
    assert snmp_type_of("DisplayString") == "DisplayString"
    assert snmp_type_of("SomethingUnknown") == "gauge"  # safe default


def test_inventory_basics(inv):
    assert inv.get("devBytesIn").snmp_type == "counter"
    assert inv.get("devBytesIn").table == "devTable"
    assert inv.get("devBytesIn").index_names == ["devIndex"]
    assert inv.get("devState").enum_values == {"1": "up", "2": "down"}
    assert [o.name for o in inv.label_candidates("devTable")] == ["devName"]


def test_augments_index_inheritance(inv):
    """A row declared AUGMENTS inherits the base row's INDEX."""
    assert inv.get("devHCBytesIn").index_names == ["devIndex"]
    assert inv.get("devHCBytesIn").table == "devXTable"


def test_unreadable_objects_excluded(inv):
    names = {o.name for o in inv.metric_candidates()}
    assert "devSecret" not in names
    assert "devBytesIn" in names


def test_counter_name_normalization():
    assert normalize_metric_name("x_bytes", "counter") == "x_bytes_total"
    assert normalize_metric_name("x_bytes_total", "counter") == "x_bytes_total"
    assert normalize_metric_name("x_bytes_total", "gauge") == "x_bytes"


def test_timeticks_name_normalization():
    """TimeTicks values are scaled to seconds, so names must say _seconds
    regardless of what the model called them."""
    assert normalize_metric_name("uptime", "gauge", "TimeTicks") == "uptime_seconds"
    assert normalize_metric_name("uptime_seconds", "gauge", "TimeTicks") == "uptime_seconds"
    assert normalize_metric_name("uptime_centiseconds", "gauge", "TimeTicks") == "uptime_seconds"
    assert normalize_metric_name("uptime_ticks", "gauge", "TimeTicks") == "uptime_seconds"
    assert normalize_metric_name("uptime_ms", "gauge", "TimeStamp") == "uptime_seconds"
    # longest suffix wins: _timeticks must not leave a stray "time" behind
    assert normalize_metric_name("last_change_timeticks", "gauge", "TimeTicks") == "last_change_seconds"
    assert normalize_metric_name("x_centiseconds", "gauge", "TimeInterval") == "x_seconds"
    assert normalize_metric_name("x_msec", "gauge", "TimeTicks") == "x_seconds"


def test_timeticks_gets_scale(inv):
    """Without scale: 0.01 the value would be centiseconds under a
    _seconds name — wrong by 100x and invisible to structural validation."""
    sel = SnmpSelection(
        module_name="dev_mib",
        metrics=[SnmpMetric(mib_object="devUptime", metric_name="widget_uptime")],
    )
    doc = yaml.safe_load(compile_snmp_yaml(inv, sel))
    m = doc["modules"]["dev_mib"]["metrics"][0]
    assert m["name"] == "widget_uptime_seconds"
    assert m["scale"] == 0.01
    # and the IR must expect the same name
    assert selection_to_ir(inv, sel, "dev").metrics[0].name == "widget_uptime_seconds"


def test_non_time_metrics_get_no_scale(inv, selection):
    doc = yaml.safe_load(compile_snmp_yaml(inv, selection))
    for m in doc["modules"]["dev_mib"]["metrics"]:
        assert "scale" not in m, m["name"]


def test_compile_snmp_yaml(inv, selection):
    doc = yaml.safe_load(compile_snmp_yaml(inv, selection))
    mod = doc["modules"]["dev_mib"]
    by_name = {m["name"]: m for m in mod["metrics"]}

    # counter convention applied even though the selection omitted _total
    assert "widget_receive_bytes_total" in by_name
    assert by_name["widget_receive_bytes_total"]["type"] == "counter"
    # scalars carry no indexes
    assert "indexes" not in by_name["widgets"]
    # table metrics get indexes + the lookup
    m = by_name["widget_receive_bytes_total"]
    assert m["indexes"] == [{"labelname": "devIndex", "type": "gauge"}]
    assert m["lookups"][0]["labelname"] == "widget"
    assert m["lookups"][0]["oid"] == "1.3.6.1.4.1.9999.2.1.2"
    # enum keys must be ints — snmp_exporter rejects string keys
    assert by_name["widget_state"]["enum_values"] == {1: "up", 2: "down"}
    assert all(isinstance(k, int) for k in by_name["widget_state"]["enum_values"])
    # lookup source OID is walked, else labels come back empty
    assert "1.3.6.1.4.1.9999.2.1.2" in mod["walk"]
    assert doc["auths"]["public_v2"]["version"] == 2


def test_augmented_table_metric_gets_lookup(inv, selection):
    """The AUGMENTS table shares devIndex, so the lookup must apply there too."""
    doc = yaml.safe_load(compile_snmp_yaml(inv, selection))
    m = {x["name"]: x for x in doc["modules"]["dev_mib"]["metrics"]}
    hc = m["widget_receive_bytes_hc_total"]
    assert hc["indexes"] == [{"labelname": "devIndex", "type": "gauge"}]
    assert hc["lookups"][0]["labelname"] == "widget"


def test_ir_derived_from_selection(inv, selection):
    spec = selection_to_ir(inv, selection, "dev")
    assert spec.kind == "snmp_generator"
    by_name = {m.name: m for m in spec.metrics}
    assert by_name["widget_receive_bytes_total"].type is MetricType.counter
    assert by_name["widgets"].type is MetricType.gauge
    # IR labels match what snmp_exporter will emit: index + lookup
    assert {l.name for l in by_name["widget_receive_bytes_total"].labels} == {
        "devIndex", "widget"
    }
    assert by_name["widgets"].labels == []
    # counters get a non-negative floor
    assert by_name["widget_receive_bytes_total"].value.min == 0


def test_ir_and_yaml_names_always_agree(inv, selection):
    """The IR is the validation contract; drift from snmp.yml would make
    every run fail for the wrong reason."""
    doc = yaml.safe_load(compile_snmp_yaml(inv, selection))
    yaml_names = {m["name"] for m in doc["modules"]["dev_mib"]["metrics"]}
    ir_names = {m.name for m in selection_to_ir(inv, selection, "dev").metrics}
    assert yaml_names == ir_names


def test_compile_rejects_bad_selection(inv):
    with pytest.raises(CompileError, match="unknown MIB object"):
        compile_snmp_yaml(inv, SnmpSelection(
            module_name="m", metrics=[SnmpMetric(mib_object="nope", metric_name="x")]))
    with pytest.raises(CompileError, match="non-numeric"):
        compile_snmp_yaml(inv, SnmpSelection(
            module_name="m",
            metrics=[SnmpMetric(mib_object="devName", metric_name="x")]))
    with pytest.raises(CompileError, match="no usable metrics"):
        compile_snmp_yaml(inv, SnmpSelection(module_name="m", metrics=[]))


def test_repair_walk_uses_parent_tables(inv, selection):
    """Repair must walk parent tables, not selected leaf OIDs.

    Walking only what was selected hides the alternatives repair needs —
    that's how a device with 32-bit counters but no ifXTable ends up with
    throughput metrics dropped instead of substituted.
    """
    from miagent.snmp.runner import repair_walk_oids

    oids = repair_walk_oids(inv, selection)
    # devTable and devXTable subtrees, not the individual column OIDs
    assert "1.3.6.1.4.1.9999.2" in oids
    assert "1.3.6.1.4.1.9999.3" in oids
    assert "1.3.6.1.4.1.9999.2.1.3" not in oids  # leaf column, covered by table
    # the scalar has no table, so it is walked directly
    assert "1.3.6.1.4.1.9999.1" in oids


def test_repair_walk_prunes_covered_subtrees(inv):
    from miagent.snmp.runner import repair_walk_oids

    sel = SnmpSelection(
        module_name="m",
        metrics=[
            SnmpMetric(mib_object="devBytesIn", metric_name="a"),
            SnmpMetric(mib_object="devState", metric_name="b"),
        ],
    )
    oids = repair_walk_oids(inv, sel)
    # both columns live in devTable: one subtree, not two
    assert oids == ["1.3.6.1.4.1.9999.2"]


def test_generator_yaml_shape(inv, selection):
    text = compile_generator_yaml(inv, selection)
    doc = yaml.safe_load(text)
    mod = doc["modules"]["dev_mib"]
    assert "devBytesIn" in mod["walk"]
    assert mod["lookups"][0]["source_indexes"] == ["devIndex"]
    assert mod["lookups"][0]["lookup"] == "devName"
    # renames are recorded as annotations for human maintainers
    assert mod["overrides"]["devBytesIn"]["rename_to"] == "widget_receive_bytes_total"
    assert "MIB modules required" in text
