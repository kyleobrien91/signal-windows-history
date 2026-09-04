/**
 * extract_dom_structure.js
 * 
 * Paste this directly into Signal Desktop's DevTools Console (Ctrl + Shift + I).
 * It analyzes the DOM, identifies all scrollable containers, outlines the hierarchy,
 * and automatically copies the clean report to your clipboard!
 */
(() => {
  // 1. Detect all scrollable containers
  const scrollables = [];
  document.querySelectorAll('*').forEach(el => {
    if (el.scrollHeight > el.clientHeight && el.clientHeight > 100) {
      scrollables.push({
        tag: el.tagName.toLowerCase(),
        id: el.id || undefined,
        className: typeof el.className === 'string' ? el.className.trim() : undefined,
        dimensions: `${el.clientWidth}x${el.clientHeight}px (scrollHeight: ${el.scrollHeight}px, scrollTop: ${el.scrollTop}px)`,
        element: el
      });
    }
  });

  // 2. Generate a clean DOM tree outline
  function dumpTree(node, depth = 0) {
    if (!node || depth > 8) return "";
    const indent = "  ".repeat(depth);
    const tag = node.tagName ? node.tagName.toLowerCase() : "";
    if (!tag || ["script", "style", "svg", "path", "defs"].includes(tag)) return "";

    const id = node.id ? `#${node.id}` : "";
    const cls = (typeof node.className === "string" && node.className.trim())
      ? `.${node.className.trim().split(/\s+/).slice(0, 4).join(".")}`
      : "";
    const role = node.getAttribute("role") ? ` [role="${node.getAttribute("role")}"]` : "";
    const testid = node.getAttribute("data-testid") ? ` [data-testid="${node.getAttribute("data-testid")}"]` : "";
    const isScrollable = (node.scrollHeight > node.clientHeight && node.clientHeight > 100)
      ? ` >>> [SCROLLABLE CONTAINER: clientHeight=${node.clientHeight}px, scrollHeight=${node.scrollHeight}px]`
      : "";

    let line = `${indent}<${tag}${id}${cls}${role}${testid}>${isScrollable}\n`;

    const children = Array.from(node.children);
    if (children.length > 8) {
      // Sample list to avoid unmanageable wall of text
      for (let i = 0; i < 2; i++) line += dumpTree(children[i], depth + 1);
      line += `${indent}  ... [${children.length - 4} more <${children[0].tagName.toLowerCase()}> items] ...\n`;
      for (let i = children.length - 2; i < children.length; i++) line += dumpTree(children[i], depth + 1);
    } else {
      for (const child of children) {
        line += dumpTree(child, depth + 1);
      }
    }
    return line;
  }

  const report = [
    "==================== 1. SCROLLABLE CONTAINERS ====================",
    JSON.stringify(scrollables.map(({element, ...rest}) => rest), null, 2),
    "\n==================== 2. CONVERSATION DOM TREE ====================",
    dumpTree(document.body)
  ].join("\n");

  console.log(report);

  if (typeof copy === "function") {
    copy(report);
    console.log("%c>>> SUCCESS: DOM structure copied to your clipboard! Paste it into our chat.", "color: #00ff66; font-size: 14px; font-weight: bold;");
  } else {
    console.log("Tip: Select and copy the text output above.");
  }
  return "Done!";
})();
