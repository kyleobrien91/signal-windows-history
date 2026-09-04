/**
 * auto_download_media.js - Signal Desktop Media Auto-Downloader
 * 
 * Specifically calibrated for:
 *   Scroll Container : main.module-timeline__messages__container
 *   Attachment Items : div.module-message__attachment-container
 * 
 * Paste this directly into the Signal DevTools Console (Ctrl + Shift + I).
 */
(async function runSignalMediaAutoDownloader() {
  const CONTAINER_SELECTOR = 'main.module-timeline__messages__container';
  const ATTACHMENT_SELECTOR = '.module-message__attachment-container';
  const container = document.querySelector(CONTAINER_SELECTOR);

  if (!container) {
    console.error(`[Error] Could not find scroll container: ${CONTAINER_SELECTOR}`);
    alert("Scroll container not found! Make sure you have opened a conversation/group in Signal first.");
    return;
  }

  // Remove existing overlay if re-running
  const existingHud = document.getElementById('signal-autodownload-hud');
  if (existingHud) existingHud.remove();

  // Create HUD overlay
  const hud = document.createElement('div');
  hud.id = 'signal-autodownload-hud';
  hud.style.cssText = `
    position: fixed;
    top: 50px;
    right: 20px;
    z-index: 999999;
    background: rgba(18, 20, 24, 0.95);
    color: #ffffff;
    border: 1px solid #3b82f6;
    border-radius: 10px;
    padding: 14px 18px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
    min-width: 260px;
  `;
  hud.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
      <b style="color:#60a5fa; font-size:14px;">⚡ Signal Media Downloader</b>
      <div>
        <button id="hud-down-btn" style="background:#3b82f6; color:#fff; border:none; border-radius:4px; padding:2px 8px; cursor:pointer; font-size:11px; margin-right:4px;">⬇ Down</button>
        <button id="hud-stop-btn" style="background:#ef4444; color:#fff; border:none; border-radius:4px; padding:2px 8px; cursor:pointer; font-size:11px;">Stop</button>
      </div>
    </div>
    <div id="hud-status" style="margin-bottom:6px;">Status: Initializing...</div>
    <div id="hud-progress" style="color:#9ca3af; font-size:12px; margin-bottom:4px;">Position: 0 / 0</div>
    <div id="hud-stats" style="color:#34d399; font-size:12px;">Media Seen: 0 | Buttons Clicked: 0</div>
  `;
  document.body.appendChild(hud);

  let isRunning = true;
  let skipToDownward = false;

  document.getElementById('hud-stop-btn').onclick = () => {
    isRunning = false;
    document.getElementById('hud-status').innerText = 'Status: Stopped by user';
  };

  document.getElementById('hud-down-btn').onclick = () => {
    skipToDownward = true;
    document.getElementById('hud-status').innerText = 'Status: Switching to downward sweep...';
  };

  const statusEl = document.getElementById('hud-status');
  const progressEl = document.getElementById('hud-progress');
  const statsEl = document.getElementById('hud-stats');

  let totalAttachmentsSeen = new Set();
  let totalButtonsClicked = 0;

  // Helper to click any explicit download buttons rendered in view
  function clickVisibleDownloadButtons() {
    const downloadBtns = container.querySelectorAll(
      'button[aria-label*="download" i], ' +
      '[class*="download-button"], ' +
      '[class*="downloadButton"], ' +
      '[class*="download"]:not(main)'
    );

    downloadBtns.forEach(btn => {
      // Avoid clicking media playback or other unrelated controls
      if (btn.tagName === 'BUTTON' && !btn.dataset.autoClicked && !btn.disabled) {
        btn.dataset.autoClicked = 'true';
        btn.click();
        totalButtonsClicked++;
      }
    });

    // Track visible attachment containers
    const attachments = container.querySelectorAll(ATTACHMENT_SELECTOR);
    attachments.forEach(el => totalAttachmentsSeen.add(el));
  }

  const delay = (ms) => new Promise(res => setTimeout(res, ms));

  console.log("%c[Signal Downloader] Starting media collection...", "color: #3b82f6; font-weight: bold;");

  // Step 1: Upward Sweep (from current position to the very top)
  statusEl.innerText = "Status: Sweeping upwards (loading older media)...";
  let lastScrollTop = container.scrollTop;
  let topStuckCount = 0;

  while (isRunning && !skipToDownward) {
    clickVisibleDownloadButtons();

    progressEl.innerText = `Scroll: ${Math.round(container.scrollTop)}px / ${container.scrollHeight}px`;
    statsEl.innerText = `Media Seen: ${totalAttachmentsSeen.size} | Buttons Clicked: ${totalButtonsClicked}`;

    // Scroll up in 450px steps
    container.scrollTop -= 450;
    await delay(180);

    // Subpixel-safe top check: <= 15px or barely moved
    const atTop = container.scrollTop <= 15;
    const notMoving = Math.abs(container.scrollTop - lastScrollTop) < 5;

    if (atTop || notMoving) {
      topStuckCount++;
      await delay(300); // Allow Signal pagination to fetch older items if any
      // If we are at the top and stayed stuck for 3 checks, older history has completed loading
      if ((atTop && topStuckCount >= 3) || topStuckCount >= 6) {
        console.log("[Signal Downloader] Reached top of conversation history. Switching to downward sweep.");
        break;
      }
    } else {
      topStuckCount = 0;
      lastScrollTop = container.scrollTop;
    }
  }

  if (!isRunning) return;

  // Step 2: Downward Sweep (from top back to bottom to ensure all mounted items trigger)
  statusEl.innerText = "Status: Sweeping downwards to bottom...";
  let bottomStuckCount = 0;
  lastScrollTop = container.scrollTop;

  while (isRunning) {
    clickVisibleDownloadButtons();

    progressEl.innerText = `Scroll: ${Math.round(container.scrollTop)}px / ${container.scrollHeight}px`;
    statsEl.innerText = `Media Seen: ${totalAttachmentsSeen.size} | Buttons Clicked: ${totalButtonsClicked}`;

    // Scroll down in 500px steps
    container.scrollTop += 500;
    await delay(150);

    const maxScroll = container.scrollHeight - container.clientHeight;
    const atBottom = container.scrollTop >= maxScroll - 20;
    const notMoving = Math.abs(container.scrollTop - lastScrollTop) < 5;

    if (atBottom || notMoving) {
      bottomStuckCount++;
      if (bottomStuckCount >= 3) {
        console.log("[Signal Downloader] Reached bottom of conversation history.");
        break;
      }
    } else {
      bottomStuckCount = 0;
      lastScrollTop = container.scrollTop;
    }
  }

  // Final check
  clickVisibleDownloadButtons();
  statusEl.innerHTML = `<span style="color:#34d399; font-weight:bold;">✔ Complete!</span> All media triggered.`;
  statsEl.innerText = `Total Media Found: ${totalAttachmentsSeen.size} | Buttons Clicked: ${totalButtonsClicked}`;
  console.log(`%c[Signal Downloader] Complete! Encountered ${totalAttachmentsSeen.size} attachment containers.`, "color: #10b981; font-weight: bold;");

  // Auto-hide HUD after 8 seconds
  setTimeout(() => {
    if (hud.parentNode) hud.remove();
  }, 8000);
})();
