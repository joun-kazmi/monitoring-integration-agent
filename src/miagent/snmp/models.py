"""Selection model — the single LLM-produced artifact in the SNMP path.

Everything downstream (snmp.yml, generator.yml, the metric IR) is compiled
from this deterministically, so this schema is deliberately small: the
model picks *what to monitor* and *what to call it*, not how to encode it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SnmpLookup(BaseModel):
    """Attach a human-readable label to every row of a table.

    e.g. index_labels=["ifIndex"], source_object="ifDescr",
    label_name="interface" turns `{ifIndex="2"}` into
    `{ifIndex="2", interface="eth0"}`.
    """

    index_labels: list[str] = Field(
        description="Index label name(s) to look up by, e.g. ['ifIndex']"
    )
    source_object: str = Field(
        description="MIB object name supplying the readable value, e.g. 'ifDescr'"
    )
    label_name: str = Field(
        description="Prometheus label to emit, e.g. 'interface' (snake_case)"
    )


class SnmpMetric(BaseModel):
    mib_object: str = Field(description="MIB object name to scrape, e.g. 'ifInOctets'")
    metric_name: str = Field(
        description="Prometheus metric name in snake_case with unit suffix, "
        "e.g. 'network_interface_receive_bytes_total'"
    )
    help: str = Field(default="", description="One-line help string")


class SnmpSelection(BaseModel):
    module_name: str = Field(
        description="snmp.yml module key in snake_case, e.g. 'if_mib'"
    )
    metrics: list[SnmpMetric]
    lookups: list[SnmpLookup] = Field(default_factory=list)
    notes: str = ""
