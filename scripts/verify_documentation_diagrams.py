"""Render and verify every maintained diagram in real Chromium without network access.

Run with ``uv run --group browser python scripts/verify_documentation_diagrams.py``.
Uses a locally installed Chrome when available, otherwise Playwright's installed Chromium.
This is documentation export tooling, not a product frontend build.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs/diagrams"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


async def verify(*, output: Path, export: bool) -> dict[str, Any]:
    """Check real font usage, offline resources, SVG bounds, skins and mobile scrolling."""
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    report: dict[str, Any] = {"network": "offline", "diagrams": [], "errors": []}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=str(CHROME) if CHROME.is_file() else None)
        report["browser"] = browser.version
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1, offline=True)
        page = await context.new_page()
        external: list[str] = []
        page.on("request", lambda request: external.append(request.url) if request.url.startswith(("https:", "http:")) else None)
        for diagram in manifest["diagrams"]:
            result: dict[str, Any] = {"slug": diagram["slug"], "skins": []}
            html_path = ROOT / diagram["artifacts"]["html"]
            await page.goto(html_path.as_uri())
            await page.evaluate("document.fonts.ready")
            result["fonts"] = await page.evaluate("Array.from(document.fonts, f => ({family:f.family,weight:f.weight,status:f.status}))")
            session = await context.new_cdp_session(page)
            await session.send("DOM.enable")
            await session.send("CSS.enable")
            document = await session.send("DOM.getDocument")
            result["rendered_fonts"] = {}
            for selector in (".diagram-title", ".node-label"):
                node = await session.send("DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": selector})
                result["rendered_fonts"][selector] = (await session.send("CSS.getPlatformFontsForNode", {"nodeId": node["nodeId"]}))["fonts"]
            await session.detach()
            for selector, fonts in result["rendered_fonts"].items():
                expected = "Jura" if selector == ".diagram-title" else "Inter"
                if not any(expected in font["familyName"] and font["isCustomFont"] for font in fonts):
                    report["errors"].append(f"{diagram['slug']}: {expected} is not actually rendered offline")
            result["overflow"] = await page.locator("svg").evaluate("""svg => {
                const box=svg.viewBox.baseVal;
                return Array.from(svg.querySelectorAll('text')).flatMap(text=>{
                    const r=text.getBBox();
                    return r.x < 0 || r.y < 0 || r.x+r.width > box.width || r.y+r.height > box.height ? [text.textContent] : [];
                });
            }""")
            if result["overflow"]:
                report["errors"].append(f"{diagram['slug']}: text exceeds SVG bounds: {result['overflow']}")
            result["node_text_overflow"] = await page.locator("svg").evaluate("""svg => Array.from(svg.querySelectorAll('.node')).flatMap(node => {
                const shape=node.querySelector('rect,polygon').getBBox();
                return Array.from(node.querySelectorAll('text')).flatMap(text=>{
                    const r=text.getBBox();
                    return r.x < shape.x+5 || r.y < shape.y+5 || r.x+r.width > shape.x+shape.width-5 || r.y+r.height > shape.y+shape.height-5 ? [node.id+': '+text.textContent] : [];
                });
            })""")
            result["diamond_geometry"] = await page.locator("svg").evaluate("""svg => {
                const problems=[];
                svg.querySelectorAll('.node polygon').forEach(polygon=>{
                    const node=polygon.parentElement, box=polygon.getBBox(), cx=box.x+box.width/2,cy=box.y+box.height/2;
                    const distance=(x,y)=>Math.abs((x-cx)/(box.width/2))+Math.abs((y-cy)/(box.height/2));
                    node.querySelectorAll('tspan').forEach(span=>{const b=span.getBBox();if([[b.x,b.y],[b.x+b.width,b.y],[b.x,b.y+b.height],[b.x+b.width,b.y+b.height]].some(([x,y])=>distance(x,y)>1.001))problems.push(node.id+': text crosses polygon');});
                    const id=node.id.split('-node-')[1];
                    const ports=svg.querySelectorAll('.connector');
                    ports.forEach(path=>{
                        const a=path.getPointAtLength(0),b=path.getPointAtLength(path.getTotalLength());
                        for(const point of [a,b]){
                            if(point.x>=box.x-1 && point.x<=box.x+box.width+1 && point.y>=box.y-1 && point.y<=box.y+box.height+1 && Math.abs(distance(point.x,point.y)-1)>.025)problems.push(node.id+': connector inside polygon bbox but off edge');
                        }
                    });
                });
                return problems;
            }""")
            if result["diamond_geometry"]:
                report["errors"].append(f"{diagram['slug']}: {result['diamond_geometry']}")
            result["key_collisions"] = await page.locator("svg").evaluate("""svg => {
                const keys=Array.from(svg.querySelectorAll('.edge-key'));
                const boxes=keys.map(key=>key.getBBox());
                const collisions=[];
                const overlaps=(a,b)=>a.x < b.x+b.width+6 && a.x+a.width+6 > b.x && a.y < b.y+b.height+6 && a.y+a.height+6 > b.y;
                boxes.forEach((a,i)=>boxes.forEach((b,j)=>{if(i<j && overlaps(a,b)) collisions.push(keys[i].textContent+' / '+keys[j].textContent);}));
                boxes.forEach((a,i)=>svg.querySelectorAll('.node').forEach(node=>{if(overlaps(a,node.getBBox()))collisions.push(keys[i].textContent+' / '+node.id);}));
                return collisions;
            }""")
            if result["node_text_overflow"] or result["key_collisions"]:
                report["errors"].append(
                    f"{diagram['slug']}: node text overflow {result['node_text_overflow']}; keyed label collisions {result['key_collisions']}"
                )
            result["html_text_geometry"] = await page.locator("svg").evaluate(
                "svg => Array.from(svg.querySelectorAll('text'), text => {const b=text.getBBox();return [text.textContent,...[b.x,b.y,b.width,b.height].map(v=>Math.round(v*100)/100)];})"
            )
            for skin in ("light", "dark"):
                await page.emulate_media(color_scheme=skin)
                result["skins"].append(
                    {"skin": skin, "background": await page.locator(".canvas").first.evaluate("node => getComputedStyle(node).fill")}
                )
                if export and skin == "light":
                    png = ROOT / diagram["artifacts"]["png"]
                    await page.locator("svg").screenshot(path=str(png))
                    diagram["png_sha256"] = hashlib.sha256(png.read_bytes()).hexdigest()
                    diagram["preview_source_sha256"] = diagram["source_sha256"]
                    diagram["preview_svg_sha256"] = diagram["svg_sha256"]
                await page.locator("svg").screenshot(path=str(output / f"{diagram['slug']}-{skin}.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            result["mobile"] = await page.evaluate(
                "({body:document.body.scrollWidth,viewport:innerWidth,scroll:document.querySelector('.diagram-scroll').scrollWidth})"
            )
            if result["mobile"]["body"] > result["mobile"]["viewport"]:
                report["errors"].append(f"{diagram['slug']}: page overflows mobile viewport outside diagram scroller")
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.emulate_media(color_scheme="light", media="print")
            pdf = await page.pdf(prefer_css_page_size=True, print_background=True)
            result["print_pages"] = len(re.findall(rb"/Type\s*/Page\b", pdf))
            (output / f"{diagram['slug']}-print.pdf").write_bytes(pdf)
            if result["print_pages"] != 1:
                report["errors"].append(f"{diagram['slug']}: print export spans {result['print_pages']} pages")
            await page.emulate_media(color_scheme="light", media="screen")
            await page.goto((ROOT / diagram["artifacts"]["svg"]).as_uri())
            await page.evaluate("document.fonts.ready")
            result["svg_text_geometry"] = await page.locator("svg").evaluate(
                "svg => Array.from(svg.querySelectorAll('text'), text => {const b=text.getBBox();return [text.textContent,...[b.x,b.y,b.width,b.height].map(v=>Math.round(v*100)/100)];})"
            )
            if result["svg_text_geometry"] != result["html_text_geometry"]:
                report["errors"].append(f"{diagram['slug']}: standalone SVG text/font geometry differs from HTML")
            result["svg_fonts"] = await page.evaluate("Array.from(document.fonts, f => ({family:f.family,status:f.status}))")
            if not any(font["family"] == "Jura" and font["status"] == "loaded" for font in result["svg_fonts"]):
                report["errors"].append(f"{diagram['slug']}: standalone SVG does not load embedded Jura")
            report["diagrams"].append(result)
        report["external_requests"] = sorted(set(external))
        if external:
            report["errors"].append("Diagram requested external resources while offline")
        await browser.close()
    if export:
        (ASSETS / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    """Export reviewed static previews and retain detailed browser receipts separately."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--export", action="store_true", help="refresh committed PNGs and their manifest digests")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(verify(output=args.output, export=args.export))
    print(json.dumps({"diagrams": len(report["diagrams"]), "errors": report["errors"]}, indent=2))  # noqa: T201
    return bool(report["errors"])


if __name__ == "__main__":
    raise SystemExit(main())
