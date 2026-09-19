"""Run snmp_exporter against a target and validate. Deterministic.

Also provides `walk_target` — a real SNMP walk used as ground truth in the
repair loop, so the selection stage can see which OIDs the device actually
implements instead of guessing.
"""

from __future__ import annotations

import shutil
import urllib.parse
from pathlib import Path
from typing import Optional

from miagent.config import settings
from miagent.ir import IntegrationSpec
from miagent.runner import RunResult, run_process_and_validate
from miagent.snmp.compile import DEFAULT_AUTH
from miagent.validate.report import Failure, FailureKind, ValidationReport


def exporter_binary() -> Optional[str]:
    """Locate snmp_exporter: configured path, ./bin, or PATH."""
    for cand in (settings.snmp_exporter_path, "./bin/snmp_exporter", "snmp_exporter"):
        if not cand:
            continue
        if Path(cand).exists():
            return str(Path(cand).resolve())
        found = shutil.which(cand)
        if found:
            return found
    return None


def scrape_url(port: int, target: str, module: str, auth: str = DEFAULT_AUTH) -> str:
    q = urllib.parse.urlencode({"target": target, "module": module, "auth": auth})
    return f"http://127.0.0.1:{port}/snmp?{q}"


def run_snmp_and_validate(
    config_path: Path,
    spec: IntegrationSpec,
    target: str,
    module: str,
    workdir: Path,
    port: int = 9116,
    auth: str = DEFAULT_AUTH,
) -> RunResult:
    binary = exporter_binary()
    if binary is None:
        return RunResult(
            report=ValidationReport(
                ok=False,
                metrics_expected=len(spec.metrics),
                failures=[
                    Failure(
                        kind=FailureKind.scrape_error,
                        detail="snmp_exporter binary not found; set MIAGENT_SNMP_EXPORTER_PATH "
                        "or place it at ./bin/snmp_exporter",
                    )
                ],
            ),
            process_log="",
        )
    cmd = [
        binary,
        f"--config.file={config_path}",
        f"--web.listen-address=127.0.0.1:{port}",
    ]
    url = scrape_url(port, target, module, auth)
    # snmp_exporter answers /snmp only when a scrape succeeds, so point the
    # readiness probe at the real scrape URL rather than a health endpoint.
    return run_process_and_validate(
        cmd, url, spec, workdir / "snmp_exporter.log", settle_s=0.2
    )


def repair_walk_oids(inv, selection, max_subtrees: int = 8) -> list[str]:
    """Choose OID subtrees for the repair walk.

    Walk the *parent table* of each selected column, not the column OID
    itself. A failure is usually "the device doesn't implement this
    object", and the fix needs to show what the device *does* implement —
    including columns the previous selection never picked (e.g. the 32-bit
    ifInOctets fallback when the 64-bit ifHCInOctets is absent). Walking
    only selected leaves hides exactly the alternatives repair needs.
    """
    subtrees: list[str] = []
    for m in selection.metrics:
        obj = inv.get(m.mib_object)
        if obj is None or not obj.oid:
            continue
        subtrees.append(obj.table_oid or obj.oid)
    for lk in selection.lookups:
        src = inv.get(lk.source_object)
        if src is not None and src.oid:
            subtrees.append(src.table_oid or src.oid)

    # Drop subtrees already covered by a shorter prefix, then cap.
    uniq = sorted(set(s for s in subtrees if s), key=lambda o: [int(p) for p in o.split(".")])
    pruned: list[str] = []
    for oid in uniq:
        if not any(oid.startswith(p + ".") for p in pruned):
            pruned.append(oid)
    return pruned[:max_subtrees] or ["1.3.6.1.2.1"]


def walk_target(
    target: str,
    oids: list[str],
    community: str = "public",
    max_rows_per_oid: int = 60,
    timeout_s: float = 3.0,
) -> str:
    """SNMP-walk the given OID subtrees; returns a readable text sample.

    Used as repair ground truth: an OID absent here is not implemented by
    the device, which is the single most common reason a selection fails.
    """
    try:
        import asyncio

        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            walk_cmd,
        )
    except ImportError:
        return "[pysnmp not installed — no walk available]"

    host, _, port_s = target.partition(":")
    port = int(port_s or 161)

    async def _walk_one(oid: str) -> list[str]:
        rows: list[str] = []
        engine = SnmpEngine()
        transport = await UdpTransportTarget.create((host, port), timeout=timeout_s, retries=1)
        try:
            async for errInd, errStat, _errIdx, varBinds in walk_cmd(
                engine,
                CommunityData(community, mpModel=1),
                transport,
                ContextData(),
                ObjectType(ObjectIdentity(oid)),
                lexicographicMode=False,
            ):
                if errInd or errStat:
                    rows.append(f"  [error: {errInd or errStat.prettyPrint()}]")
                    break
                for vb in varBinds:
                    rows.append(f"  {vb[0].prettyPrint()} = {vb[1].prettyPrint()[:80]}")
                if len(rows) >= max_rows_per_oid:
                    rows.append("  [...]")
                    break
        except Exception as e:  # network/timeout/library differences
            rows.append(f"  [walk failed: {type(e).__name__}: {e}]")
        finally:
            engine.close_dispatcher()
        return rows

    async def _walk_all() -> str:
        out: list[str] = []
        for oid in oids:
            out.append(f"{oid}:")
            out.extend(await _walk_one(oid))
        return "\n".join(out)

    try:
        return asyncio.run(_walk_all())
    except Exception as e:
        return f"[walk failed: {type(e).__name__}: {e}]"
