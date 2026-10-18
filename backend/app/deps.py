"""Builds the process-wide dependencies (store, data sources, Gemini) from settings. Used by the web
app, the standalone worker and the evaluation script."""
from __future__ import annotations

import logging

from .config import Settings
from .orchestrator import Deps
from .sources import Sources
from .store import build_store

log = logging.getLogger(__name__)


def build_deps(s: Settings) -> Deps:
    gemini = None
    if (s.gemini_mode == "vertex" and (s.gemini_project or s.google_cloud_project)) or \
            (s.gemini_mode == "api_key" and s.gemini_api_key):
        try:
            from .gemini import Gemini
            gemini = Gemini(s)
        except Exception as e:              # misconfigured AI must not stop the product: checks run without it
            log.warning("Gemini disabled: %s", e)
    return Deps(settings=s, store=build_store(s), sources=Sources.build(s), gemini=gemini)
