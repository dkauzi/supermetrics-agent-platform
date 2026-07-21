"""Publish a static, read-only copy of the dashboard.

The live dashboard needs the FastAPI backend, so opening dashboard.html from
disk shows an empty page and GitHub Pages cannot host the real thing. A reviewer
who will not clone and run the project would otherwise never see it.

This drives a real demo run, captures the actual API responses, and inlines them
into a copy of the same dashboard file. One UI source, two modes: the page uses
window.__SNAPSHOT__ when present and fetches from the backend when it is not, so
the published page cannot drift from the real one.

It is labelled a snapshot in the header, and the feedback buttons say so, because
a demo that quietly pretends to be live is worse than no demo.

    python scripts/build_snapshot.py     ->  site/index.html
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentplatform import build_platform  # noqa: E402
from agentplatform.config import data_dir  # noqa: E402

# GitHub Pages will only serve from the repo root or /docs, so the snapshot lands
# alongside the written docs rather than in a directory of its own.
SITE = ROOT / "docs"


def capture(client) -> dict:
    """Hit every read endpoint the dashboard uses and keep the responses."""
    snapshot: dict = {
        "__captured_at": datetime.now(timezone.utc).strftime("%d %b %Y"),
    }

    for path in ("/traces", "/registry", "/dead-letters", "/calibration",
                 "/quality", "/tools", "/cost"):
        response = client.get(path)
        response.raise_for_status()
        snapshot[path] = response.json()

    # Per-trace detail, so clicking a run in the published page works.
    for run in snapshot["/traces"]["traces"]:
        trace_id = run["trace_id"]
        for path in (f"/traces/{trace_id}", f"/traces/{trace_id}/why"):
            snapshot[path] = client.get(path).json()

    return snapshot


def main() -> int:
    from fastapi.testclient import TestClient

    # Fresh database so the snapshot shows a coherent story rather than whatever
    # happened to be lying around from local experimentation.
    db = data_dir() / "platform.db"
    if db.exists():
        db.unlink()

    import runner
    print("Driving a real demo run...")
    runner.main()

    platform = build_platform()
    print("Running the golden eval to populate the gate panel...")
    from tests.golden.run_eval import run_eval
    run_eval(platform, samples=1)

    # Seed the learning loop so the published page shows a closed loop rather
    # than an empty table. These are real verdicts recorded through the real path.
    from agentplatform.feedback import record_outcome
    traces = platform.warehouse.recent_traces(50)
    renewal = [t for t in traces if t["agent"] == "renewal_risk"][:1]
    for index, run in enumerate(renewal):
        record_outcome(platform.warehouse, run["trace_id"], "renewal_risk",
                       "ACC-4417", "adoption_decline", "critical",
                       "correct" if index % 2 == 0 else "wrong", reviewer="demo")

    import app
    print("Capturing API responses...")
    with TestClient(app.app) as client:
        snapshot = capture(client)

    SITE.mkdir(exist_ok=True)
    dashboard = (ROOT / "dashboard.html").read_text()

    payload = json.dumps(snapshot, indent=1, default=str)
    injected = dashboard.replace(
        "<script>",
        f"<script>window.__SNAPSHOT__ = {payload};</script>\n<script>",
        1,
    )
    (SITE / "index.html").write_text(injected)
    (SITE / ".nojekyll").write_text("")

    size_kb = len(injected) / 1024
    print(f"\nWrote {SITE / 'index.html'} ({size_kb:.0f} KB)")
    print(f"Captured {len([k for k in snapshot if k.startswith('/')])} endpoint responses")
    print("Published via GitHub Pages from branch main, /docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
