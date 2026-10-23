"""Offline fictional activity and explicitly modeled ROI, not business outcomes."""

from .core import build_report, rounded, scenario
from .view import export_report, render_html

__all__ = ["build_report", "export_report", "render_html", "rounded", "scenario"]
