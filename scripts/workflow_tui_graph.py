"""Graph tab rendering for the workflow TUI (phart dependency visualization)."""

from __future__ import annotations

from typing import Any

from rich import box
from rich.panel import Panel
from rich.text import Text

try:
    import networkx as nx
    from phart import ASCIIRenderer, LayoutOptions
    HAS_PHART = True
except ImportError:
    HAS_PHART = False

STATUS_ICONS = {
    "completed": "\u25cf",
    "running": "\u25d0",
    "pending": "\u25cb",
    "failed": "\u2717",
    "cancelled": "\u2298",
    "blocked": "\u25c9",
    "paused": "\u2016",
}

# Animated running icons cycle through these on each refresh
_RUNNING_CYCLE = ["\u25d0", "\u25d3", "\u25d1", "\u25d2"]

# ANSI color names for phart node_color attribute
STATUS_COLORS = {
    "completed": "green",
    "running": "cyan",
    "pending": "yellow",
    "failed": "red",
    "cancelled": "bright_black",
    "blocked": "magenta",
    "paused": "yellow",
}


def build_run_graph(run: dict[str, Any]) -> Any | None:
    """Build a NetworkX DiGraph from workflow run state."""
    if not HAS_PHART:
        return None
    import time as _time
    G = nx.DiGraph()
    agents = run.get("agents", [])
    phases = run.get("phases", [])
    if not agents:
        return None
    # Animated running icon cycles based on current time (use snapshot time when available)
    from workflow_tui_render import snapshot_reference_time
    snapshot_now = snapshot_reference_time()
    effective_time = snapshot_now.timestamp() if snapshot_now else _time.time()
    cycle_index = int(effective_time) % len(_RUNNING_CYCLE)
    # Add start node
    title = str(run.get("title", "workflow")[:20])
    G.add_node("\u25b6", label=title, color="white")
    # Build agent nodes with compact labels and colors
    for agent in agents:
        name = str(agent.get("name", ""))
        status = str(agent.get("status") or "pending")
        if status == "running":
            icon = _RUNNING_CYCLE[cycle_index]
        else:
            icon = STATUS_ICONS.get(status, STATUS_ICONS["pending"])
        # Short label: just name + icon, no runner type
        label = f"{name} {icon}"
        color = STATUS_COLORS.get(status, STATUS_COLORS["pending"])
        G.add_node(name, label=label[:24], color=color)
    # Connect start to agents with no dependencies
    for agent in agents:
        depends_on = str(agent.get("depends_on", "")).strip()
        if not depends_on:
            G.add_edge("\u25b6", str(agent.get("name", "")))
    # Connect dependencies
    for agent in agents:
        name = str(agent.get("name", ""))
        depends_on = str(agent.get("depends_on", "")).strip()
        if not depends_on:
            continue
        for dep in depends_on.split(","):
            dep = dep.strip()
            if dep and dep in G:
                G.add_edge(dep, name)
    return G if len(G) > 1 else None


def make_run_graph_panel(run: dict[str, Any], detail_width: int | None = None) -> Any:
    """Render a dependency graph for a workflow run using phart."""
    if not HAS_PHART:
        return Panel(
            Text("Install phart for graph view: pip install phart", style="dim"),
            title="Dependency Graph",
            border_style="yellow",
            box=box.ROUNDED,
        )
    G = build_run_graph(run)
    if G is None:
        return Panel(
            Text("No agents to graph.", style="dim"),
            title="Dependency Graph",
            border_style="yellow",
            box=box.ROUNDED,
        )
    # Account for panel borders/padding (\u22488 chars). Clamp to a sane range.
    if detail_width is None:
        target_width = 76
    else:
        target_width = max(40, min(120, detail_width - 8))
    opts = LayoutOptions(
        use_labels=True,
        node_label_attr="label",
        bboxes=True,
        hpad=2,
        vpad=1,
        layer_spacing=3,
        use_ascii=False,
        ansi_colors=True,
        target_canvas_width=target_width,
    )
    renderer = ASCIIRenderer(G, options=opts)
    graph_text = renderer.render()
    # Center each line horizontally within the available width.
    import re as _re
    _ansi_re = _re.compile(r"\x1b\[[0-9;]*m")
    centered_lines = []
    for line in graph_text.splitlines():
        visible = _ansi_re.sub("", line)
        pad = max(0, (target_width - len(visible)) // 2)
        centered_lines.append(" " * pad + line)
    graph_text = "\n".join(centered_lines)
    return Panel(
        Text.from_ansi(graph_text, overflow="crop", no_wrap=True),
        title="Dependency Graph",
        border_style="cyan",
        box=box.ROUNDED,
    )
