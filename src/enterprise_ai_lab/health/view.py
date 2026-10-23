"""Private, self-contained exports. No network assets or client-side arithmetic."""

from __future__ import annotations

import hashlib
import html
import json
import os
import stat
from pathlib import Path

from ..costs.contracts import MAX_OUTPUT, CostError


def _json_bytes(report):
    try:
        data = (
            json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode()
    except (ValueError, TypeError, RecursionError):
        raise CostError("invalid_input") from None
    if len(data) > MAX_OUTPUT:
        raise CostError("capacity_exceeded")
    return data


def render_html(report):
    """Escape every displayed value and the inert JSON data block separately."""
    raw = _json_bytes(report).decode()
    inert = (
        raw.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    esc = lambda value: html.escape(str(value), quote=True)
    current, previous = report["current"], report["previous"]
    base, cost = report["modeled_base"], report["modeled_cost"]
    roi = lambda cell: (
        "Undefined: zero modeled cost"
        if cell["roi_percent"] is None
        else esc(cell["roi_percent"]["display"]) + "%"
    )
    usd = lambda value: "USD " + esc(value["display_usd"])
    cards = "".join(
        f"<article><h3>{esc(label)}</h3><strong>{esc(value)}</strong><p>{esc(note)}</p></article>"
        for label, value, note in (
            (
                "Previous usage-identified requests",
                previous["usage_identified_requests"],
                "Distinct request IDs in the previous fictional interval.",
            ),
            (
                "Current usage-identified requests",
                current["usage_identified_requests"],
                "Distinct request IDs; not completed or successful tasks.",
            ),
            (
                "Current fictional ledger cost",
                "USD " + current["ledger_cost"]["display_usd"],
                "Recorded fictional charges; aggregate display rounding.",
            ),
        )
    )
    rows = []
    for cell in report["sensitivity"]:
        levels = cell["levels"]
        rows.append(
            "<tr>"
            + "".join(
                f"<td>{esc(levels[key])}</td>"
                for key in ("baseline_minutes", "automation_share", "realization_share")
            )
            + f"<td>{usd(cell['gross_benefit'])}</td><td>{usd(cell['net_benefit'])}</td><td>{roi(cell)}</td></tr>"
        )
    assumptions = report["assumptions"]
    assumption_rows = "".join(
        f'<tr><th scope="row">{esc(key)}</th>'
        + "".join(
            f"<td>{esc(assumptions[key][level])}</td>"
            for level in ("low", "base", "high")
        )
        + "</tr>"
        for key in ("baseline_minutes", "automation_share", "realization_share")
    )
    metrics = "".join(
        f'<tr><th scope="row">{esc(key)}</th><td>{esc(value["event_count"])}</td><td>{esc(value["quantity"])}</td><td>{usd(value["charge"])}</td></tr>'
        for key, value in current["metrics"].items()
    )
    signals = "".join(
        f"<li><b>{esc(key)}</b>: {esc(str(value).lower())}</li>"
        for key, value in report["activity"]["flags"].items()
    )
    limits = "".join(f"<li>{esc(item)}</li>" for item in report["limitations"])
    change = report["activity"]["percent_change_display"]
    change = (
        "Undefined: previous count is zero" if change is None else esc(change) + "%"
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Fictional usage and modeled ROI — {esc(report["scenario_id"])}</title>
<style>
:root{{color-scheme:light;--ink:#201a35;--purple:#6540c8;--coral:#f58275;--mint:#c9f2dc}}*{{box-sizing:border-box}}body{{margin:0;background:#faf8ff;color:var(--ink);font:16px/1.6 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:clamp(16px,4vw,48px)}}header{{border-left:8px solid var(--purple);padding:12px 24px;background:#ece5ff;border-radius:12px}}h1{{font-size:clamp(1.7rem,4vw,2.6rem);line-height:1.2}}h2{{margin-top:2em}}.badge{{font-weight:700;background:var(--mint);display:inline-block;padding:3px 12px;border-radius:20px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px;margin-top:20px}}article{{background:white;border:1px solid #c9bfdc;border-top:5px solid var(--purple);border-radius:12px;padding:18px}}article h3{{font-size:1rem;margin:0 0 10px}}strong{{font-size:1.5rem}}.modeled{{background:#fff0ed;border:2px solid var(--coral);padding:20px;border-radius:12px}}.scroll{{overflow:auto}}.scroll:focus-visible{{outline:3px solid var(--purple);outline-offset:3px}}table{{width:100%;border-collapse:collapse;background:white;font-size:.9rem}}th,td{{text-align:left;padding:9px;border-bottom:1px solid #ded7ea}}thead{{background:var(--mint)}}caption{{text-align:left;font-weight:700;padding:10px 0}}code{{overflow-wrap:anywhere}}a{{color:#4b299d}}.note{{font-size:.9rem}}footer{{margin-top:30px;border-top:2px solid var(--purple)}}
</style></head><body><main>
<header><span class="badge">Offline fictional study</span><h1>Usage, assumptions, and modeled ROI</h1><p>Tenant <b>{esc(report["snapshot"]["tenant_id"])}</b> · Scenario <b>{esc(report["scenario_id"])}</b></p><p>Fictional ledger activity is separate from assumed business benefit. No measured savings or predictive health score.</p></header>
<section aria-labelledby="usage"><h2 id="usage">Recorded fictional usage</h2><p>Previous [{esc(previous["start"])}, {esc(previous["end"])})<br>Current [{esc(current["start"])}, {esc(current["end"])}) · equal {esc(report["period_seconds"])}-second windows.</p><div class="cards">{cards}</div><p>Request-count change: {esc(report["activity"]["request_count_change"])} · relative change: {change}.</p><ul>{signals}</ul><p class="note">These authored activity signals count usage-identified requests. They do not measure successful tasks, satisfaction, retention, or causality.</p><p class="note">On narrow screens, focus or swipe a table to scroll horizontally.</p><div class="scroll" tabindex="0" role="region" aria-label="Current fictional metric table"><table><caption>Current fictional metric quantities and charges</caption><thead><tr><th>Metric</th><th>Events</th><th>Quantity</th><th>Charge</th></tr></thead><tbody>{metrics}</tbody></table></div></section>
<section aria-labelledby="model"><h2 id="model">Modeled business benefit</h2><div class="modeled"><p>Base gross benefit: <b>{usd(base["gross_benefit"])}</b> · net benefit: <b>{usd(base["net_benefit"])}</b> · ROI: <b>{roi(base)}</b></p><p>Total modeled cost: {usd(cost["total"])} = ledger {usd(cost["ledger"])} + allocated fixed {usd(cost["allocated_fixed"])} + period operating {usd(cost["period_operating"])}.</p><p>Hourly rate assumption: USD {esc(assumptions["hourly_rate"])}. Authored minimum requests: {esc(assumptions["minimum_requests"])}.</p><p>Low/base/high values are scenario ranges, not statistical confidence intervals.</p></div><div class="scroll" tabindex="0" role="region" aria-label="Scenario assumption table"><table><caption>Authored assumptions; minutes per request and dimensionless shares</caption><thead><tr><th>Assumption</th><th>Low</th><th>Base</th><th>High</th></tr></thead><tbody>{assumption_rows}</tbody></table></div><p><code>{esc(report["formula"])}</code></p><div class="scroll" tabindex="0" role="region" aria-label="Sensitivity table"><table><caption>All 27 precomputed sensitivity combinations</caption><thead><tr><th>Minutes</th><th>Automation</th><th>Realization</th><th>Gross benefit</th><th>Net benefit</th><th>ROI</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>
<section aria-labelledby="trace"><h2 id="trace">Traceable snapshot</h2><p>One validated tenant read. Watermark: {esc(report["snapshot"]["watermark"])}. Selected rows SHA256: <code>{esc(report["snapshot"]["snapshot_sha256"])}</code>.</p><p>Rate-card SHA256: <code>{esc(report["snapshot"]["rate_card_sha256"])}</code>. <a href="report.json" download>Download exact fractions, assumptions, source hashes, and event references</a>.</p><ul>{limits}</ul></section>
<footer><p>{esc(report["rounding"])}</p><p>This private artifact contains the selected tenant's fictional usage references. It has no external assets, network calls, or browser-side floating-point calculation.</p></footer>
<script id="report-data" type="application/json">{inert}</script>
</main></body></html>"""
    if len(document.encode()) > MAX_OUTPUT:
        raise CostError("capacity_exceeded")
    return document


def export_report(report, out_dir):
    data = _json_bytes(report)
    document = render_html(report).encode()
    path = Path(out_dir).absolute()
    descriptor = None
    try:
        path.mkdir(mode=0o700)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
            raise CostError("storage_unavailable")
        os.fchmod(descriptor, 0o700)
        for filename, contents in (("report.json", data), ("index.html", document)):
            fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise CostError("storage_unavailable")
        os.fsync(descriptor)
    except FileExistsError:
        raise CostError("invalid_input") from None
    except OSError:
        raise CostError("storage_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return {
        "report_sha256": hashlib.sha256(data).hexdigest(),
        "html_sha256": hashlib.sha256(document).hexdigest(),
        "snapshot_sha256": report["snapshot"]["snapshot_sha256"],
        "cells": 27,
    }
