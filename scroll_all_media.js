/**
 * scroll_all_media.js (Pro HUD & Real-Time Telemetry Edition)
 * 
 * Features a real-time HUD with live metrics:
 * 1. Live Cooldown Countdown: Visual countdown timer showing remaining pause time.
 * 2. Active Download Detection: Counts in-flight download spinners / busy indicators.
 * 3. Memory Telemetry: Monitors live V8 JS Heap usage via performance.memory.
 * 4. ETA & Velocity: Estimates time remaining and calculates scroll throughput.
 * 5. Animated Progress Bar: Visual completion bar based on scrollHeight.
 * 6. Interactive Controls: [Pause / Resume], [Mode: Normal / Fast], and [Stop].
 */
(async function runTelemetryAllMediaScroller() {
  function findActiveContainer() {
    const candidates = Array.from(document.querySelectorAll('*')).filter(el => {
      return el.scrollHeight > el.clientHeight + 40 && el.clientHeight > 250;
    });
    candidates.sort((a, b) => (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight));
    return candidates[0] || null;
  }

  const container = findActiveContainer();
  if (!container) {
    alert("Could not find a scrollable container. Please make sure the 'All Media' view is open (Ctrl + Shift + M)!");
    return;
  }

  // Remove existing HUD if present
  const oldHud = document.getElementById('signal-media-hud');
  if (oldHud) oldHud.remove();

  // Create Professional Dashboard HUD
  const hud = document.createElement('div');
  hud.id = 'signal-media-hud';
  hud.style.cssText = `
    position: fixed;
    top: 30px;
    right: 20px;
    z-index: 999999;
    background: rgba(15, 23, 42, 0.96);
    color: #f8fafc;
    border: 1px solid #3b82f6;
    border-radius: 12px;
    padding: 16px 20px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 12.5px;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.6);
    width: 330px;
    user-select: none;
    backdrop-filter: blur(10px);
    transition: all 0.2s ease;
  `;

  hud.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
      <span style="font-weight:700; color:#60a5fa; font-size:14px; display:flex; align-items:center; gap:6px;">
        <span style="display:inline-block; width:8px; height:8px; background:#10b981; border-radius:50%; box-shadow:0 0 8px #10b981;"></span>
        Media Telemetry HUD
      </span>
      <div style="display:flex; gap:5px;">
        <button id="hud-pause-btn" style="background:#1e293b; color:#38bdf8; border:1px solid #475569; border-radius:5px; padding:3px 8px; cursor:pointer; font-size:11px;">Pause</button>
        <button id="hud-speed-btn" style="background:#1e293b; color:#f59e0b; border:1px solid #475569; border-radius:5px; padding:3px 8px; cursor:pointer; font-size:11px;">Normal</button>
        <button id="hud-stop-btn" style="background:#ef4444; color:#fff; border:none; border-radius:5px; padding:3px 8px; cursor:pointer; font-size:11px;">Stop</button>
      </div>
    </div>

    <!-- Progress Bar -->
    <div style="background:#334155; border-radius:6px; height:7px; width:100%; overflow:hidden; margin-bottom:12px;">
      <div id="hud-progress-bar" style="background:linear-gradient(90deg, #3b82f6, #10b981); width:0%; height:100%; transition:width 0.2s ease;"></div>
    </div>

    <!-- Status -->
    <div id="hud-status" style="font-weight:600; color:#e2e8f0; margin-bottom:10px; font-size:13px;">
      Status: <span style="color:#38bdf8;">Starting scan...</span>
    </div>

    <!-- Live Telemetry Grid -->
    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:8px 12px; background:rgba(30, 41, 59, 0.6); padding:10px 12px; border-radius:8px; margin-bottom:10px; border:1px solid #334155;">
      <div>
        <div style="color:#94a3b8; font-size:11px;">PROGRESS</div>
        <div id="hud-pct" style="font-weight:700; color:#f1f5f9; font-size:14px;">0%</div>
      </div>
      <div>
        <div style="color:#94a3b8; font-size:11px;">EST. REMAINING</div>
        <div id="hud-eta" style="font-weight:700; color:#a78bfa; font-size:14px;">Calculating...</div>
      </div>
      <div>
        <div style="color:#94a3b8; font-size:11px;">DOWNLOADS IN-FLIGHT</div>
        <div id="hud-active-spinners" style="font-weight:700; color:#fbbf24; font-size:14px;">0 active</div>
      </div>
      <div>
        <div style="color:#94a3b8; font-size:11px;">BUTTONS CLICKED</div>
        <div id="hud-clicks" style="font-weight:700; color:#34d399; font-size:14px;">0</div>
      </div>
    </div>

    <!-- Engine & Queue Diagnostics -->
    <div style="font-size:11.5px; color:#94a3b8; display:flex; flex-direction:column; gap:4px;">
      <div style="display:flex; justify-content:space-between;">
        <span>Position:</span>
        <span id="hud-scroll-info" style="color:#cbd5e1; font-family:monospace;">0 / 0 px</span>
      </div>
      <div style="display:flex; justify-content:space-between;">
        <span>Engine Cooldown:</span>
        <span id="hud-cooldown-info" style="color:#38bdf8;">Next in 16 steps</span>
      </div>
      <div style="display:flex; justify-content:space-between;">
        <span>V8 JS Heap Memory:</span>
        <span id="hud-memory-info" style="color:#cbd5e1; font-family:monospace;">-- MB</span>
      </div>
    </div>
  `;

  document.body.appendChild(hud);

  // Control State
  let isRunning = true;
  let isPaused = false;
  let isFastMode = false;

  const pauseBtn = document.getElementById('hud-pause-btn');
  pauseBtn.onclick = () => {
    isPaused = !isPaused;
    pauseBtn.innerText = isPaused ? 'Resume' : 'Pause';
    pauseBtn.style.color = isPaused ? '#34d399' : '#38bdf8';
    document.getElementById('hud-status').innerHTML = isPaused
      ? `Status: <span style="color:#fbbf24;">⏸️ Paused by user</span>`
      : `Status: <span style="color:#38bdf8;">Resuming scan...</span>`;
  };

  const speedBtn = document.getElementById('hud-speed-btn');
  speedBtn.onclick = () => {
    isFastMode = !isFastMode;
    speedBtn.innerText = isFastMode ? 'Fast' : 'Normal';
    speedBtn.style.color = isFastMode ? '#f59e0b' : '#38bdf8';
  };

  document.getElementById('hud-stop-btn').onclick = () => {
    isRunning = false;
    document.getElementById('hud-status').innerHTML = `Status: <span style="color:#ef4444;">⏹ Stopped</span>`;
  };

  // UI Element Refs
  const statusEl = document.getElementById('hud-status');
  const progressBar = document.getElementById('hud-progress-bar');
  const pctEl = document.getElementById('hud-pct');
  const etaEl = document.getElementById('hud-eta');
  const spinnersEl = document.getElementById('hud-active-spinners');
  const clicksEl = document.getElementById('hud-clicks');
  const scrollInfoEl = document.getElementById('hud-scroll-info');
  const cooldownInfoEl = document.getElementById('hud-cooldown-info');
  const memoryInfoEl = document.getElementById('hud-memory-info');

  let clickedCount = 0;
  let stepCounter = 0;
  const startTime = Date.now();

  function getMemoryMB() {
    if (window.performance && window.performance.memory) {
      return (window.performance.memory.usedJSHeapSize / (1024 * 1024)).toFixed(1);
    }
    return null;
  }

  function countActiveDownloads() {
    // Count spinning indicators, progress bars, or busy flags in Signal's DOM
    const spinners = document.querySelectorAll(
      '[class*="spinner"], [class*="progress"], [class*="loading"], [role="progressbar"], [aria-busy="true"]'
    );
    return spinners.length;
  }

  function triggerDownloadButtons() {
    const buttons = container.querySelectorAll(
      'button[aria-label*="download" i]:not([data-ac]), [class*="download-button"]:not([data-ac])'
    );
    for (let i = 0; i < buttons.length; i++) {
      const btn = buttons[i];
      if (!btn.disabled) {
        btn.setAttribute('data-ac', '1');
        btn.click();
        clickedCount++;
      }
    }
  }

  const delay = (ms) => new Promise(res => setTimeout(res, ms));

  let lastScrollTop = container.scrollTop;
  let stuckCount = 0;

  console.log("%c[Telemetry Scroller] Running with live metrics...", "color: #3b82f6; font-weight: bold;");

  while (isRunning) {
    if (isPaused) {
      await delay(200);
      continue;
    }

    stepCounter++;
    triggerDownloadButtons();

    const maxScroll = container.scrollHeight - container.clientHeight;
    const currentScroll = Math.max(0, container.scrollTop);
    const pct = maxScroll > 0 ? Math.min(100, Math.round((currentScroll / maxScroll) * 100)) : 100;

    // Update Progress & Telemetry UI
    progressBar.style.width = `${pct}%`;
    pctEl.innerText = `${pct}%`;
    clicksEl.innerText = clickedCount;
    scrollInfoEl.innerText = `${Math.round(currentScroll).toLocaleString()} / ${container.scrollHeight.toLocaleString()} px`;

    // Active in-flight downloads
    const activeDownloads = countActiveDownloads();
    spinnersEl.innerHTML = activeDownloads > 0
      ? `<span style="color:#fbbf24; animation: pulse 1s infinite;">${activeDownloads} active</span>`
      : `<span style="color:#94a3b8;">0 idle</span>`;

    // JS Heap Memory
    const memMB = getMemoryMB();
    if (memMB) {
      const memColor = parseFloat(memMB) > 350 ? '#ef4444' : '#cbd5e1';
      memoryInfoEl.innerHTML = `<span style="color:${memColor};">${memMB} MB</span>`;
    }

    // ETA Calculation
    const elapsedSec = (Date.now() - startTime) / 1000;
    if (pct > 2 && pct < 100) {
      const totalEstimatedSec = (elapsedSec / (pct / 100));
      const remainingSec = Math.max(0, Math.round(totalEstimatedSec - elapsedSec));
      if (remainingSec >= 60) {
        etaEl.innerText = `~${Math.floor(remainingSec / 60)}m ${remainingSec % 60}s`;
      } else {
        etaEl.innerText = `~${remainingSec}s`;
      }
    } else if (pct >= 100) {
      etaEl.innerText = `Complete`;
    }

    // Steps until next cooldown
    const stepsUntilCooldown = 16 - (stepCounter % 16);
    cooldownInfoEl.innerText = `Next in ${stepsUntilCooldown} steps`;

    // ----------------------------------------------------------------
    // Cooldown Sequence with Live Countdown Timer
    // ----------------------------------------------------------------
    if (stepCounter % 16 === 0) {
      const cooldownMs = isFastMode ? 800 : 1500;
      const cooldownStart = Date.now();

      while (Date.now() - cooldownStart < cooldownMs) {
        const remainingMs = Math.max(0, cooldownMs - (Date.now() - cooldownStart));
        const remSec = (remainingMs / 1000).toFixed(1);

        statusEl.innerHTML = `Status: <span style="color:#fbbf24;">⏳ Cooling down (${remSec}s left)...</span>`;
        cooldownInfoEl.innerHTML = `<span style="color:#fbbf24;">Draining I/O queue... ${remSec}s</span>`;

        // Update active downloads during cooldown
        spinnersEl.innerText = `${countActiveDownloads()} active`;
        await delay(100);
      }

      statusEl.innerHTML = `Status: <span style="color:#38bdf8;">Sweeping downwards...</span>`;
      cooldownInfoEl.innerText = `Next in 16 steps`;
    }

    // Jump viewport
    const jumpSize = Math.max(450, Math.floor(container.clientHeight * 0.85));
    container.scrollTop += jumpSize;
    await delay(isFastMode ? 140 : 250);

    const atBottom = container.scrollTop >= maxScroll - 30;
    const notMoving = Math.abs(container.scrollTop - lastScrollTop) < 5;

    if (atBottom || notMoving) {
      stuckCount++;
      statusEl.innerHTML = `Status: <span style="color:#a78bfa;">Fetching older media batch...</span>`;
      await delay(450); // Allow infinite scroll pagination

      const newMax = container.scrollHeight - container.clientHeight;
      if (atBottom && container.scrollTop >= newMax - 30 && stuckCount >= 4) {
        console.log("[Telemetry Scroller] Reached end of media history.");
        break;
      }
    } else {
      stuckCount = 0;
      lastScrollTop = container.scrollTop;
    }
  }

  // Completion State
  triggerDownloadButtons();
  progressBar.style.width = '100%';
  pctEl.innerText = '100%';
  etaEl.innerText = '0s';
  statusEl.innerHTML = `<span style="color:#34d399; font-weight:700;">✔ Complete!</span> Scanned full media gallery.`;
  cooldownInfoEl.innerHTML = `<span style="color:#34d399;">Queue cleared</span>`;

  console.log(`%c[Telemetry Scroller] Complete! Clicks: ${clickedCount}`, "color: #10b981; font-weight: bold;");
  setTimeout(() => { if (hud.parentNode) hud.remove(); }, 12000);
})();
