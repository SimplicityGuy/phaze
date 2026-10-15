"""Render the source-cited Phaze diagram manifest to self-contained HTML and SVG.

The editable manifest describes components and relationships, not a layout language.
Orthogonal paths avoid every node; bounded legends preserve long guards without tiny text.
PNG previews are produced by the separate browser verifier from these exact HTML sources.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import heapq
import html
import importlib.util
from itertools import pairwise
import json
from pathlib import Path
import sys
import textwrap
from typing import Any, cast


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.diagram_inputs import render_input_digest  # noqa: E402


ASSETS = ROOT / "docs/diagrams"
VENDOR = ROOT / "scripts/vendor/diagram_design"
MANIFEST = ASSETS / "manifest.json"


def escaped(value: str) -> str:
    """Escape text for HTML and XML alike."""
    return html.escape(value, quote=True)


def wrap(value: str, width: int = 25) -> list[str]:
    """Wrap all content without dropping labels or splitting identifier spelling."""
    return textwrap.wrap(value, width=width, break_long_words=True, break_on_hyphens=False) or [""]


def text_lines(x: float, y: float, lines: list[str], *, css: str = "node-label", step: int = 23) -> str:
    """Emit SVG text with explicit stable line positions."""
    return (
        f'<text class="{css}" x="{x:g}" y="{y:g}">'
        + "".join(f'<tspan x="{x:g}" dy="{0 if i == 0 else step}">{escaped(line)}</tspan>' for i, line in enumerate(lines))
        + "</text>"
    )


def masked_label(x: float, y: float, label: str) -> str:
    """Reserve an opaque label mask with a clear gap around crossing strokes."""
    return f'<g class="edge-key"><rect class="canvas" x="{x - 6:g}" y="{y - 16:g}" width="{len(label) * 9 + 12}" height="24"/>{text_lines(x, y, [label], css="edge-label")}</g>'


def rounded_path(points: list[list[float]], radius: float = 8) -> str:
    """Round orthogonal bends with the design grammar's eight-pixel corner radius."""
    points = [point for i, point in enumerate(points) if i == 0 or point != points[i - 1]]
    result = f"M {points[0][0]:g} {points[0][1]:g}"
    for index in range(1, len(points) - 1):
        before, corner, after = points[index - 1 : index + 2]
        incoming = abs(corner[0] - before[0]) + abs(corner[1] - before[1])
        outgoing = abs(after[0] - corner[0]) + abs(after[1] - corner[1])
        r = min(radius, incoming / 2, outgoing / 2)
        start = [corner[j] + (before[j] - corner[j]) * r / incoming for j in range(2)]
        end = [corner[j] + (after[j] - corner[j]) * r / outgoing for j in range(2)]
        result += f" L {start[0]:g} {start[1]:g} Q {corner[0]:g} {corner[1]:g} {end[0]:g} {end[1]:g}"
    return result + f" L {points[-1][0]:g} {points[-1][1]:g}"


def sequence_drawing(diagram: dict[str, Any]) -> tuple[str, int, int]:
    """Show compact lifelines, activation intervals and typed call/return messages."""
    slug = diagram["slug"]
    nodes, edges = diagram["nodes"], diagram["edges"]
    width, height = 1100, 690
    centers = {node["id"]: 190 + i * 360 for i, node in enumerate(nodes)}
    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="{slug}-title {slug}-desc">',
        f'<title id="{slug}-title">{escaped(diagram["title"])}</title><desc id="{slug}-desc">{escaped(diagram["summary"])}</desc>',
        f'<defs><marker id="{slug}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" class="arrow-head"/></marker></defs>',
        f'<rect class="canvas" width="{width}" height="{height}"/>',
        text_lines(65, 45, ["SEQUENCE"], css="eyebrow"),
        text_lines(65, 89, [diagram["title"]], css="diagram-title"),
    ]
    for node in nodes:
        x = centers[node["id"]]
        chunks.append(
            f'<g id="{slug}-node-{node["id"]}" class="node" tabindex="0" role="group" aria-label="{escaped(node["label"])}"><rect x="{x - 125}" y="125" width="250" height="65" rx="6"/>{text_lines(x - 107, 156, wrap(node["label"], 24))}</g>'
        )
        chunks.append(f'<path d="M {x} 190 L {x} 655" class="lifeline"/>')
    for participant, top, bottom in [("API", 230, 440), ("API", 500, 615), ("DB", 320, 360), ("DB", 590, 630)]:
        x = centers[participant]
        chunks.append(f'<rect class="activation" x="{x - 4}" y="{top}" width="8" height="{bottom - top}"/>')
    for i, edge in enumerate(edges):
        y = 245 + i * 87
        a, b = centers[edge["source"]], centers[edge["target"]]
        kind = " return" if i == 2 else ""
        chunks.append(f'<path id="{edge["id"]}" class="connector{kind}" d="M {a} {y} L {b} {y}" marker-end="url(#{slug}-arrow)"/>')
        chunks.append(text_lines(min(a, b) + 15, y - 41, wrap(edge["label"], 43), css="legend", step=19))
    return "\n".join([*chunks, "</svg>"]), width, height


def grid_layout(diagram: dict[str, Any]) -> None:
    """Route compact component clusters on a clearance grid with separated edge ports."""
    nodes, edges = diagram["nodes"], diagram["edges"]
    degree = Counter(endpoint for edge in edges for endpoint in (edge["source"], edge["target"]))
    positions: dict[str, tuple[int, int, int, int]] = {}
    y = 180
    for row in range((len(nodes) + 2) // 3):
        heights = []
        for column, node in enumerate(nodes[row * 3 : row * 3 + 3]):
            height = max(108, 48 + len(wrap(node["label"], 26)) * 23, 36 + degree[node["id"]] * 12)
            height = max(height, 180) if "?" in node["label"] and diagram["type"] == "flowchart" else height
            height = ((height + 11) // 12) * 12
            node["position"] = [72 + column * 456, y, 300, height]
            positions[node["id"]] = tuple(node["position"])
            if "?" in node["label"] and diagram["type"] == "flowchart":
                node["decision"] = True
            heights.append(height)
        y += max(heights) + 144
    columns, rows = 124, (y + 72) // 12
    blocked: set[tuple[int, int]] = set()
    for x, top, width, height in positions.values():
        blocked.update(
            (gx, gy) for gx in range((x - 8) // 12, (x + width + 8) // 12 + 1) for gy in range((top - 8) // 12, (top + height + 8) // 12 + 1)
        )
    occupied: Counter[tuple[tuple[int, int], ...]] = Counter()
    used: Counter[str] = Counter()
    for edge in edges:
        ax, ay, aw, _ah = positions[edge["source"]]
        bx, by, _bw, _bh = positions[edge["target"]]
        used[edge["source"]] += 1
        used[edge["target"]] += 1
        sy = ay + 24 + used[edge["source"]] * 12
        ty = by + 24 + used[edge["target"]] * 12
        start, finish = ((ax + aw + 24) // 12, sy // 12), ((bx - 24) // 12, ty // 12)
        initial = (start[0], start[1], 0, 0)
        queue = [(0, 0, initial)]
        costs = {initial: 0}
        parents: dict[tuple[int, int, int, int], tuple[int, int, int, int]] = {}
        serial = 0
        found = None
        while queue:
            _, cost, state = heapq.heappop(queue)
            xg, yg, dx, dy = state
            if cost != costs[state]:
                continue
            if (xg, yg) == finish:
                found = state
                break
            for nx, ny in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                point = (xg + nx, yg + ny)
                if not (1 <= point[0] < columns and 11 <= point[1] < rows) or point in blocked:
                    continue
                segment = tuple(sorted(((xg, yg), point)))
                next_cost = cost + 1 + (6 if (dx or dy) and (nx, ny) != (dx, dy) else 0) + occupied[segment] * 30
                nxt = (*point, nx, ny)
                if next_cost >= costs.get(nxt, 10**9):
                    continue
                costs[nxt] = next_cost
                parents[nxt] = state
                serial += 1
                heuristic = abs(point[0] - finish[0]) + abs(point[1] - finish[1])
                heapq.heappush(queue, (next_cost + heuristic, next_cost, nxt))
        if found is None:
            raise ValueError(f"No clear route for {edge['id']}")
        path = [found]
        while path[-1] in parents:
            path.append(parents[path[-1]])
        path.reverse()
        points = [[ax + aw, sy], *[[state[0] * 12, state[1] * 12] for state in path], [bx, ty]]
        simplified = [points[0]]
        for index in range(1, len(points) - 1):
            a, b, c = points[index - 1 : index + 2]
            if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
                continue
            simplified.append(b)
        simplified.append(points[-1])
        for name, index in ((edge["source"], 0), (edge["target"], -1)):
            node = next(node for node in nodes if node["id"] == name)
            if node.get("decision"):
                left, top, width, height = positions[name]
                inset = abs(simplified[index][1] - (top + height / 2)) * width / height
                simplified[index][0] = round(left + width - inset if index == 0 else left + inset)
        edge["points"] = simplified
        for before, after in pairwise(path):
            occupied[tuple(sorted((before[:2], after[:2])))] += 1


def key_position(
    points: list[list[float]], nodes: list[tuple[float, float, float, float]], occupied: list[tuple[float, float, float, float]]
) -> tuple[float, float]:
    """Place a keyed label beside a clear route segment, away from cards and other keys."""
    candidates = []
    for a, b in pairwise(points):
        distance = abs(a[0] - b[0]) + abs(a[1] - b[1])
        for fraction in (0.5, 0.25, 0.75, 0.15, 0.85):
            x = a[0] + (b[0] - a[0]) * fraction
            y = a[1] + (b[1] - a[1]) * fraction
            candidates.append((distance, x + 12 if a[0] == b[0] else x - 13, y + 5 if a[0] == b[0] else y - 10))
            if a[0] == b[0]:
                candidates.append((distance, x - 47, y + 5))

    def overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float], gap: float = 8) -> bool:
        return a[0] < b[0] + b[2] + gap and a[0] + a[2] + gap > b[0] and a[1] < b[1] + b[3] + gap and a[1] + a[3] + gap > b[1]

    for _, x, y in sorted(candidates, reverse=True):
        box = (x - 6, y - 16, 39, 24)
        if x < 45 or y < 145 or any(overlaps(box, node) for node in nodes) or any(overlaps(box, other) for other in occupied):
            continue
        occupied.append(box)
        return x, y
    raise ValueError("No collision-free key position; split this diagram or give its routes more room")


def drawing(diagram: dict[str, Any]) -> tuple[str, int, int]:
    """Build a diagram with explicit node, edge and legend identities."""
    if diagram["type"] == "sequence":
        return sequence_drawing(diagram)
    if diagram.get("layout") == "grid":
        grid_layout(diagram)
    slug = diagram["slug"]
    nodes = diagram["nodes"]
    edges = diagram["edges"]
    # A vertical component spine with individually spaced relationship buses keeps dense
    # dependency views traceable. Every incident edge receives a distinct node port.
    degree = Counter(endpoint for edge in edges for endpoint in (edge["source"], edge["target"]))
    positions: dict[str, tuple[float, float, float, float]] = {}
    y_cursor = 155.0
    for node in nodes:
        lines = wrap(node["label"], 52)
        height = max(100, len(lines) * 23 + 42, degree[node["id"]] * 20 + 38)
        positions[node["id"]] = tuple(node.get("position", (85, y_cursor, 490, height)))
        y_cursor += height + 45
    width = (
        int(
            max(
                max(x + w for x, _, w, _ in positions.values()) + 90,
                max((max(point[0] for point in edge.get("points", [])) for edge in edges if edge.get("points")), default=0) + 100,
            )
        )
        if all("position" in n for n in nodes)
        else max(900, 650 + len(edges) * 18 + 80)
    )
    floor = max(y + h for _, y, _, h in positions.values()) + 70
    labeled = [edge for edge in edges if edge["label"]]
    legend_rows = []
    legend_y = floor + 40
    for edge in labeled:
        lines = wrap(edge["label"], 60)
        legend_rows.append((edge, legend_y, lines))
        legend_y += max(36, len(lines) * 20 + 18)
    height = int(legend_y + 50 if labeled else floor + 45)
    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="{slug}-title {slug}-desc" data-theme="light">',
        f'<title id="{slug}-title">{escaped(diagram["title"])}</title>',
        f'<desc id="{slug}-desc">{escaped(diagram["summary"])} All relationships and guards are listed below the components and in the source evidence table.</desc>',
        f'<defs><marker id="{slug}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" class="arrow-head"/></marker></defs>',
        f'<rect class="canvas" width="{width}" height="{height}"/>',
        text_lines(85, 62, [diagram["type"].upper()], css="eyebrow"),
        text_lines(85, 108, wrap(diagram["title"], 48), css="diagram-title"),
    ]
    port_use: Counter[str] = Counter()
    badges = []
    key_boxes: list[tuple[float, float, float, float]] = []
    for i, edge in enumerate(edges, 1):
        source, target = edge["source"], edge["target"]
        ax, ay, aw, ah = positions[source]
        bx, by, bw, bh = positions[target]
        port_use[source] += 1
        sy = ay + ah * port_use[source] / (degree[source] + 1)
        port_use[target] += 1
        ty = by + bh * port_use[target] / (degree[target] + 1)
        bus = 650 + i * 18
        path = rounded_path(edge.get("points", [[ax + aw, sy], [bus, sy], [bus, ty], [bx + bw, ty]]))
        chunks.append(
            f'<path id="{edge["id"]}" class="connector {edge.get("style", "")}" d="{path}" marker-end="url(#{slug}-arrow)"><title>{escaped(edge["label"] or source + " to " + target)}</title></path>'
        )
        if edge["label"]:
            if diagram["type"] == "entity-relationship":
                first, last = edge["points"][0], edge["points"][-1]
                source_left = first[0] == positions[source][0]
                target_left = last[0] == positions[target][0]
                badges.append(
                    text_lines(first[0] + (-25 if source_left else 8), first[1] - 6, [edge.get("source_cardinality", "1")], css="edge-label")
                )
                cardinality = edge.get("target_cardinality", "0..N")
                badges.append(text_lines(last[0] + (-45 if target_left else 8), last[1] - 6, [cardinality], css="edge-label"))
            points = edge.get("points", [[ax + aw, sy], [bus, sy], [bus, ty], [bx + bw, ty]])
            label_x, label_y = key_position(points, list(positions.values()), key_boxes)
            badges.append(masked_label(label_x, label_y, f"E{i:02}"))
    chunks.extend(badges)
    for node in nodes:
        x, y, w, h = positions[node["id"]]
        chunks.append(
            f'<g id="{slug}-node-{node["id"]}" class="node {node.get("treatment", "")}" tabindex="0" role="group" aria-label="{escaped(node["label"])}">'
        )
        if node.get("decision"):
            chunks.append(f'<polygon points="{x + w / 2:g},{y:g} {x + w:g},{y + h / 2:g} {x + w / 2:g},{y + h:g} {x:g},{y + h / 2:g}"/>')
        else:
            chunks.append(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{20 if node.get("terminal") else 6}"/>')
        if "fields" in node:
            chunks.append(text_lines(x + 17, y + 23, ["ENTITY"], css="eyebrow"))
            chunks.append(text_lines(x + 17, y + 49, [node["label"]]))
            chunks.append(f'<path class="field-rule" d="M {x} {y + 62} L {x + w} {y + 62}"/>')
            field_lines = [line for field in node["fields"][:4] for line in wrap(field, 43)]
            chunks.append(text_lines(x + 17, y + 85, field_lines, css="field", step=19))
        elif node.get("decision"):
            lines = wrap(node["label"], 23)
            label = text_lines(x + w / 2, y + h / 2 - (len(lines) - 1) * 11.5 + 6, lines)
            chunks.append(label.replace("<text ", '<text text-anchor="middle" ', 1))
        else:
            chunks.append(text_lines(x + 17, y + 32, wrap(node["label"], 26 if "position" in node else 52)))
        chunks.append("</g>")
    for edge, y, lines in legend_rows:
        number = edges.index(edge) + 1
        chunks.append(text_lines(85, y, [f"E{number:02}"], css="edge-label"))
        chunks.append(text_lines(140, y, lines, css="legend", step=20))
    chunks.append("</svg>")
    return "\n".join(chunks), width, height


def embedded_fonts() -> str:
    """Embed licensed Latin font bytes for both browser and standalone SVG fidelity."""
    return "".join(
        f"@font-face{{font-family:{family};font-style:normal;font-weight:{weight};font-display:block;src:url(data:font/woff2;base64,{base64.b64encode((ASSETS / 'fonts' / f'{family.lower()}-{weight}.woff2').read_bytes()).decode()}) format('woff2');}}"
        for family, weight in (("Inter", 400), ("Inter", 600), ("Jura", 300), ("Jura", 500))
    )


def css(profile: dict[str, Any]) -> str:
    """Use Phaze tokens with no mandatory external font/resource request."""

    def variables(skin: str) -> str:
        return ";".join(f"--{key}:{value}" for key, value in profile[skin].items())

    faces = embedded_fonts()
    return (
        faces
        + f"""svg,:root{{{variables("light")}}}@media(prefers-color-scheme:dark){{svg,:root{{{variables("dark")}}}}}
body{{margin:0;background:var(--paper);color:var(--ink);font:16px Inter,system-ui,sans-serif;padding:32px}}main{{max-width:1440px;margin:auto}}
h1{{font:500 42px Jura,Inter,system-ui,sans-serif}}button,a{{color:var(--accent)}}button{{font:inherit;background:var(--panel);border:1px solid var(--muted);padding:8px 14px;margin:0 8px 16px 0;cursor:pointer}}
.diagram-scroll{{overflow-x:auto;max-width:100%;border:1px solid var(--rule)}}svg{{display:block;width:var(--diagram-width);height:var(--diagram-height);min-width:var(--diagram-width);background:var(--paper)}}
.canvas{{fill:var(--paper)}}.node rect,.node polygon{{fill:var(--panel);stroke:var(--muted);stroke-width:1.5}}.node:focus rect,.node:focus polygon{{stroke:var(--accent);stroke-width:3}}
.node.focal rect{{stroke:var(--accent);stroke-width:2.5}}.activation{{fill:var(--panel);stroke:var(--muted);stroke-width:.8}}.connector.influence{{stroke:var(--muted);stroke-dasharray:5 4}}.connector.return{{stroke:var(--muted);stroke-dasharray:5 4}}
.field-rule{{fill:none;stroke:var(--rule);stroke-width:1}}.field{{fill:var(--muted);font:14px ui-monospace,monospace}}
.node-label{{fill:var(--ink);font:600 17px Inter,system-ui,sans-serif}}.diagram-title{{fill:var(--ink);font:500 32px Jura,Inter,system-ui,sans-serif}}
.eyebrow{{fill:var(--accent);font:600 14px ui-monospace,monospace;letter-spacing:2px}}.connector{{fill:none;stroke:var(--accent);stroke-width:1.5}}.arrow-head{{fill:var(--accent)}}.lifeline{{fill:none;stroke:var(--muted);stroke-width:1;stroke-dasharray:4 7}}
.edge-label{{fill:var(--accent);font:600 13px ui-monospace,monospace}}.legend{{fill:var(--ink);font:15px Inter,system-ui,sans-serif}}p,summary{{line-height:1.6}}td,th{{padding:8px;text-align:left;border-bottom:1px solid var(--rule)}}table{{border-collapse:collapse;width:100%}}code{{overflow-wrap:anywhere}}
@media print{{body{{padding:0}}button,details,.font-note,h1,main>p{{display:none}}.diagram-scroll{{overflow:visible;border:0}}svg{{min-width:0;width:100%;height:auto;max-height:370mm}}@page{{size:A3 portrait;margin:12mm}}}}
"""
    )


def render() -> dict[str, Any]:
    """Write every manifest source, standalone SVG, and deterministic source digests."""
    manifest = cast("dict[str, Any]", json.loads(MANIFEST.read_text()))
    profile = json.loads((ASSETS / "profile.json").read_text())
    export_path = VENDOR / "skills/diagram-design/scripts/export_svg.py"
    spec = importlib.util.spec_from_file_location("phaze_diagram_export", export_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for diagram in manifest["diagrams"]:
        svg, width, height = drawing(diagram)
        slug = diagram["slug"]
        style = css(profile).replace("var(--diagram-width)", f"{width}px").replace("var(--diagram-height)", f"{height}px")
        rows = "".join(
            f"<tr><td>{escaped(edge['id'])}</td><td>{escaped(edge['source'])} → {escaped(edge['target'])}: {escaped(edge['label'])}</td><td>{'<br>'.join(escaped(p) for p in edge['evidence'])}</td></tr>"
            for edge in diagram["edges"]
        )
        source = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escaped(diagram["title"])} · Phaze</title><style>{style}</style></head><body data-theme="light"><main><h1>{escaped(diagram["title"])}</h1><p>{escaped(diagram["summary"])}</p><div class="diagram-scroll">{svg}</div><p class="font-note">{escaped(profile["typography"]["offline"])}</p><details><summary>Relationships and source evidence</summary><table><thead><tr><th>Relationship</th><th>Meaning</th><th>Source</th></tr></thead><tbody>{rows}</tbody></table></details></main></body></html>\n"""
        html_path = ASSETS / f"{slug}.html"
        html_path.write_text(source)
        svg_path = ASSETS / f"{slug}.svg"
        exported = module.export_svg_document(source, html_path, system_fonts=True)
        # Upstream filters some shared SVG selectors (including eyebrow and root sizing).
        # Preserve the exact local stylesheet as well as its licensed embedded fonts.
        exported = exported.replace("<defs>", "<defs><style>" + style + "</style>", 1)
        svg_path.write_text(exported)
        diagram["artifacts"] = {
            "html": html_path.relative_to(ROOT).as_posix(),
            "svg": svg_path.relative_to(ROOT).as_posix(),
            "png": f"docs/diagrams/{slug}.png",
        }
        diagram["render_input_sha256"] = render_input_digest(ROOT, diagram)
        diagram["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        diagram["svg_sha256"] = hashlib.sha256(svg_path.read_bytes()).hexdigest()
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main() -> None:
    """Regenerate checked source assets without introducing a frontend build."""
    argparse.ArgumentParser(description=__doc__).parse_args()
    manifest = render()
    print(f"Rendered {len(manifest['diagrams'])} documentation diagrams")  # noqa: T201


if __name__ == "__main__":
    main()
