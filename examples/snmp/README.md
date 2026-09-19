# SNMP test fixtures

Simulated SNMP agents for developing and testing the SNMP path, via
`snmpsim` (`pip3 install --user snmpsim`). The **community name is the
`.snmprec` filename**.

## oldswitch.snmprec

A legacy switch that implements only `ifTable` (32-bit counters) and has
**no `ifXTable`** — so no 64-bit `ifHC*` counters and no `ifName`. Since
the selection prompt prefers 64-bit counters, this fixture reliably forces
a validation failure and exercises the repair loop, which must fall back
to the 32-bit counters and use `ifDescr` for the interface label.

```bash
snmpsim-command-responder --data-dir=examples/snmp \
    --agent-udpv4-endpoint=127.0.0.1:11162 --quiet &

PYTHONPATH=src python3 -m miagent.cli generate-snmp \
    --service oldswitch --mib IF-MIB --target 127.0.0.1:11162 \
    --community oldswitch --port 9119 --workdir build/snmp-oldswitch
```

## A fully-featured device

snmpsim ships one: copy its bundled `public.snmprec` (real IF-MIB rows
including `ifXTable` and simulated increasing counters) into a data dir.

```bash
cp ~/.local/lib/python3.10/site-packages/snmpsim/data/public.snmprec /tmp/snmpdata/
snmpsim-command-responder --data-dir=/tmp/snmpdata \
    --agent-udpv4-endpoint=127.0.0.1:11161 --quiet &
```
