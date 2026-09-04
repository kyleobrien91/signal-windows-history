#!/usr/bin/env python3
"""
dump_signal_dom.py - Connects to Signal Desktop via Chrome DevTools Protocol (CDP)
and dumps the DOM structure / HTML tree when Signal is launched with:
    Signal.exe --remote-debugging-port=9222
"""

import json
import os
import sys
import urllib.request

try:
    import websockets
    import asyncio
except ImportError:
    websockets = None


def get_cdp_targets(port=9222):
    url = f"http://127.0.0.1:{port}/json"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Error connecting to CDP at {url}: {e}")
        print("\nMake sure Signal Desktop was launched with:")
        print(r'  Start-Process "$env:LOCALAPPDATA\Programs\signal-desktop\Signal.exe" -ArgumentList "--remote-debugging-port=9222"')
        return None


async def inspect_dom(ws_url):
    import websockets
    async with websockets.connect(ws_url) as ws:
        # Script to extract the scrollable structure and DOM outline
        extract_js = """
        (() => {
            const scrollables = [];
            document.querySelectorAll('*').forEach(el => {
                if (el.scrollHeight > el.clientHeight && el.clientHeight > 100) {
                    scrollables.push({
                        tag: el.tagName.toLowerCase(),
                        id: el.id || null,
                        classes: el.className ? el.className.toString().split(/\\s+/).filter(Boolean) : [],
                        dimensions: `${el.clientWidth}x${el.clientHeight} (scrollHeight: ${el.scrollHeight}, scrollTop: ${el.scrollTop})`
                    });
                }
            });

            function dumpTree(node, depth = 0) {
                if (!node || depth > 8) return "";
                const indent = "  ".repeat(depth);
                const tag = node.tagName ? node.tagName.toLowerCase() : "";
                if (!tag || ["script", "style", "svg", "path"].includes(tag)) return "";

                const id = node.id ? `#${node.id}` : "";
                const cls = (typeof node.className === 'string' && node.className.trim())
                    ? '.' + node.className.trim().split(/\\s+/).slice(0, 3).join('.')
                    : '';
                const role = node.getAttribute('role') ? ` [role="${node.getAttribute('role')}"]` : '';
                const testid = node.getAttribute('data-testid') ? ` [data-testid="${node.getAttribute('data-testid')}"]` : '';
                const isScrollable = (node.scrollHeight > node.clientHeight && node.clientHeight > 100)
                    ? ` (SCROLLABLE: ${node.clientHeight}px / ${node.scrollHeight}px)`
                    : '';

                let out = `${indent}<${tag}${id}${cls}${role}${testid}>${isScrollable}\\n`;

                const children = Array.from(node.children);
                if (children.length > 10) {
                    for (let i = 0; i < 2; i++) out += dumpTree(children[i], depth + 1);
                    out += `${indent}  ... [${children.length - 4} more <${children[0].tagName.toLowerCase()}> items] ...\\n`;
                    for (let i = children.length - 2; i < children.length; i++) out += dumpTree(children[i], depth + 1);
                } else {
                    for (const ch of children) out += dumpTree(ch, depth + 1);
                }
                return out;
            }

            return {
                scrollables,
                tree: dumpTree(document.body)
            };
        })()
        """

        # Send CDP Runtime.evaluate command
        cmd = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": extract_js,
                "returnByValue": True
            }
        }
        await ws.send(json.dumps(cmd))
        resp = json.loads(await ws.recv())
        return resp["result"]["result"]["value"]


def main():
    targets = get_cdp_targets()
    if not targets:
        sys.exit(1)

    # Filter for main Signal window (usually type == 'page')
    page_targets = [t for t in targets if t.get("type") == "page"]
    if not page_targets:
        print("No page target found in Signal.")
        sys.exit(1)

    target = page_targets[0]
    print(f"Attached to target: {target.get('title')} ({target.get('url')})")
    ws_url = target.get("webSocketDebuggerUrl")

    if websockets is None:
        print("Please install websockets: pip install websockets")
        sys.exit(1)

    result = asyncio.run(inspect_dom(ws_url))
    
    out_file = "signal_dom_structure.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    
    print(f"\nSaved DOM analysis to: {out_file}")
    print("\n--- SCROLLABLE CONTAINERS FOUND ---")
    for s in result.get("scrollables", []):
        print(f"Tag: {s['tag']} | Classes: {s['classes']} | {s['dimensions']}")


if __name__ == "__main__":
    main()
