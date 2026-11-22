// Trackside AprilTag Lap Counter & Live Tracker - Frontend Controller & Pack-Aware TTS Engine

const supportsNativePopover = Object.hasOwn(HTMLElement.prototype, "popover");

// Light-dismiss fallback for <dialog closedby="any"> where native closedBy is absent
if (!("closedBy" in HTMLDialogElement.prototype)) {
  const outside = (e, dialog) => {
    const r = dialog.getBoundingClientRect();
    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
  };
  for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
    let pressedOutside = false;
    dialog.addEventListener("pointerdown", (e) => {
      pressedOutside = outside(e, dialog);
    });
    dialog.addEventListener("pointerup", (e) => {
      if (pressedOutside && outside(e, dialog)) dialog.close();
    });
  }
}

function attachImeSafeEnterSubmit(inputEl, formEl) {
  let lastCompositionEndAt = null;
  inputEl.addEventListener("compositionend", (e) => {
    lastCompositionEndAt = e.timeStamp;
  });
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (e.isComposing || e.keyCode === 229) return;
      if (lastCompositionEndAt !== null && Math.abs(e.timeStamp - lastCompositionEndAt) < 50) {
        return;
      }
      formEl.requestSubmit();
    }
  });
}

function formatClockMmss(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const mins = Math.floor(s / 60);
  const rem = (s - mins * 60).toFixed(1).padStart(4, "0");
  return `${String(mins).padStart(2, "0")}:${rem}`;
}

function formatSpokenDuration(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  let mins = Math.floor(s / 60);
  let rem = Math.round((s - mins * 60) * 10) / 10;
  if (rem >= 60) {
    mins += 1;
    rem = 0;
  }
  if (mins === 0) return `${rem.toFixed(1)} seconds`;
  const unit = mins === 1 ? "minute" : "minutes";
  return rem === 0 ? `${mins} ${unit}` : `${mins} ${unit} ${rem.toFixed(1)} seconds`;
}

const BURN_LINES = [
  (n, l, t) => `${n}, lap ${l} in ${t}. Did you stop to tie your shoes or check your email?`,
  (n, l) => `Lap ${l} complete for ${n}. At this pace, the sun might set before lap ten.`,
  (n) => `Bold strategy by ${n}, saving all that energy for... next week.`,
  (n, _l, t) => `${n} crosses the line in ${t}. Technically, that still counts as running.`,
];

const DUO_BURN_LINES = [
  (a, la, ta, b, lb) =>
    `Side by side! ${a} on lap ${la} in ${ta}, right with ${b} on lap ${lb}. Holding hands, or just drafting off each other's excuses?`,
  (a, _la, ta, b, _lb, tb) =>
    `Two at the line! ${a} in ${ta} and ${b} in ${tb}. Neither one wanted to suffer alone.`,
  (a, la, _ta, b, lb) =>
    `${a} and ${b} come through together on laps ${la} and ${lb}. Synced strides, questionable speed.`,
];

/**
 * Sequential + Pack-Aware Offline Web Speech API AnnouncerService
 */
class AnnouncerService {
  constructor() {
    this.synth = "speechSynthesis" in window ? window.speechSynthesis : null;
    this.queue = []; // Array of { kind: "text" | "pack", text?: string, events?: Array, burnFreq?: number }
    this.pendingPack = [];
    this.packTimer = null;
    this.speaking = false;
    this.muted = false;
    this.rate = 1.05;
    this.selectedVoiceURI = "";
    this.voices = [];
  }

  initVoices(selectEl) {
    if (!this.synth) return;
    const populate = () => {
      const all = this.synth.getVoices() || [];
      this.voices = all;
      const prev = this.selectedVoiceURI || selectEl.value;
      selectEl.innerHTML = "";

      const preferredNames = ["Samantha", "Daniel", "Karen", "Moira", "Alex", "Tessa"];
      const sorted = [...all].sort((a, b) => {
        const aEn = a.lang.toLowerCase().startsWith("en") ? 0 : 1;
        const bEn = b.lang.toLowerCase().startsWith("en") ? 0 : 1;
        if (aEn !== bEn) return aEn - bEn;
        const aPref = preferredNames.findIndex((p) => a.name.includes(p));
        const bPref = preferredNames.findIndex((p) => b.name.includes(p));
        if (aPref !== -1 || bPref !== -1) {
          return (aPref === -1 ? 99 : aPref) - (bPref === -1 ? 99 : bPref);
        }
        return a.name.localeCompare(b.name);
      });

      const defaultOpt = document.createElement("option");
      defaultOpt.value = "";
      defaultOpt.textContent = "macOS Default Voice";
      selectEl.appendChild(defaultOpt);

      for (const v of sorted) {
        const opt = document.createElement("option");
        opt.value = v.voiceURI;
        opt.textContent = `${v.name} (${v.lang})${v.localService ? "" : " · Cloud"}`;
        selectEl.appendChild(opt);
      }

      if (prev && sorted.some((v) => v.voiceURI === prev)) {
        selectEl.value = prev;
        this.selectedVoiceURI = prev;
      } else {
        const best = sorted.find(
          (v) => v.localService && preferredNames.some((p) => v.name.includes(p))
        );
        if (best) {
          selectEl.value = best.voiceURI;
          this.selectedVoiceURI = best.voiceURI;
        }
      }
    };

    populate();
    if (typeof this.synth.addEventListener === "function") {
      this.synth.addEventListener("voiceschanged", populate);
    } else {
      this.synth.onvoiceschanged = populate;
    }
  }

  enqueue(text, category = "standard") {
    updateTicker(text, category);
    if (this.muted || !this.synth || !text) return;
    this.queue.push({ kind: "text", text, category });
    this._drain();
  }

  /**
   * Collects simultaneous / tightly-staggered runner crossings (within 420ms, or while
   * another sentence is currently speaking) so groups of runners get counted AND spoken
   * to together without falling 15s behind real time.
   */
  enqueueLapEvent(lapEvt, burnFrequency) {
    this.pendingPack.push(lapEvt);
    if (this.packTimer !== null) return;
    this.packTimer = setTimeout(() => {
      this.packTimer = null;
      const batch = this.pendingPack.splice(0, this.pendingPack.length);
      if (batch.length === 0) return;

      // If currently speaking and the last waiting item in queue is an unspoken pack batch
      // of < 4 runners, merge into it so back-to-back pack arrivals get called together.
      const tail = this.queue[this.queue.length - 1];
      if (this.speaking && tail && tail.kind === "pack" && tail.events.length + batch.length <= 4) {
        tail.events.push(...batch);
        tail.burnFreq = burnFrequency;
        const preview = this.buildBatchCommentary(tail.events, tail.burnFreq);
        updateTicker(preview.text, preview.category);
        return;
      }

      const preview = this.buildBatchCommentary(batch, burnFrequency);
      updateTicker(preview.text, preview.category);
      if (this.muted || !this.synth) return;
      this.queue.push({ kind: "pack", events: batch, burnFreq: burnFrequency });
      this._drain();
    }, 420);
  }

  _drain() {
    if (this.speaking || this.queue.length === 0 || !this.synth) return;
    if (this.muted) {
      this.queue.length = 0;
      return;
    }
    const item = this.queue.shift();
    let spokenText = item.text || "";
    if (item.kind === "pack") {
      const built = this.buildBatchCommentary(item.events, item.burnFreq);
      spokenText = built.text;
      updateTicker(built.text, built.category);
    }
    if (!spokenText) {
      this._drain();
      return;
    }

    this.speaking = true;
    const utter = new SpeechSynthesisUtterance(spokenText);
    utter.rate = this.rate;
    if (this.selectedVoiceURI) {
      const match = this.voices.find((v) => v.voiceURI === this.selectedVoiceURI);
      if (match) utter.voice = match;
    }

    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(safetyTimer);
      this.speaking = false;
      this._drain();
    };

    const safetyTimer = setTimeout(finish, 14000);
    utter.onend = finish;
    utter.onerror = finish;
    try {
      this.synth.speak(utter);
    } catch {
      finish();
    }
  }

  setMuted(flag) {
    this.muted = Boolean(flag);
    if (this.muted && this.synth) {
      this.queue.length = 0;
      try {
        this.synth.cancel();
      } catch {
        // ignore
      }
      this.speaking = false;
    }
  }

  buildBatchCommentary(events, burnFrequency) {
    if (events.length === 1) {
      return this.buildSoloCommentary(events[0], burnFrequency);
    }
    const burnPct = Number(burnFrequency) || 0;
    const anyPr = events.some((e) => e.is_pr);
    const anySlow = events.some((e) => e.is_slow);
    const rollBurn = !anyPr && ((burnPct > 0 && anySlow) || (burnPct > 0 && Math.random() * 100 < burnPct));

    if (events.length === 2) {
      const [e1, e2] = events;
      const n1 = e1.runner?.name || `Tag ${e1.runner?.tag_id}`;
      const n2 = e2.runner?.name || `Tag ${e2.runner?.tag_id}`;
      const t1 = formatSpokenDuration(e1.lap_time);
      const t2 = formatSpokenDuration(e2.lap_time);

      if (anyPr) {
        const prName = e1.is_pr ? n1 : n2;
        return {
          category: "pr",
          text: `Photo finish! ${n1} on lap ${e1.lap_number} in ${t1} right with ${n2} on lap ${e2.lap_number} in ${t2}—new best lap for ${prName}!`,
        };
      }
      if (rollBurn) {
        const fn = DUO_BURN_LINES[Math.floor(Math.random() * DUO_BURN_LINES.length)];
        return {
          category: "burn",
          text: fn(n1, e1.lap_number, t1, n2, e2.lap_number, t2),
        };
      }
      return {
        category: "standard",
        text: `Together at the line! ${n1}, lap ${e1.lap_number} in ${t1}, and ${n2}, lap ${e2.lap_number} in ${t2}!`,
      };
    }

    // 3+ runners crossing together: call every single runner's name + lap count concisely
    const parts = events.map((e, idx) => {
      const nm = e.runner?.name || `Tag ${e.runner?.tag_id}`;
      const prefix = idx === events.length - 1 ? "and " : "";
      return `${prefix}${nm} on lap ${e.lap_number}`;
    });
    const roster = parts.join(", ");
    if (anyPr) {
      const prNames = events
        .filter((e) => e.is_pr)
        .map((e) => e.runner?.name)
        .join(" and ");
      return {
        category: "pr",
        text: `Pack of ${events.length} through the line! ${roster}—with a new personal best for ${prNames}!`,
      };
    }
    if (rollBurn) {
      return {
        category: "burn",
        text: `Pack of ${events.length}! ${roster}. Looks less like a race and more like a group coffee stroll.`,
      };
    }
    return {
      category: "standard",
      text: `Pack of ${events.length} across the line! ${roster}—all counted!`,
    };
  }

  buildSoloCommentary(lapEvt, burnFrequency) {
    const name = lapEvt.runner?.name || `Tag ${lapEvt.runner?.tag_id ?? 0}`;
    const laps = lapEvt.lap_number;
    const spoken = formatSpokenDuration(lapEvt.lap_time);
    const burnPct = Number(burnFrequency) || 0;

    if (lapEvt.is_pr) {
      return {
        category: "pr",
        text: `New best lap for ${name}! ${spoken}—keep that cadence!`,
      };
    }
    const rollBurn = (burnPct > 0 && lapEvt.is_slow) || (burnPct > 0 && Math.random() * 100 < burnPct);
    if (rollBurn) {
      const fn = BURN_LINES[Math.floor(Math.random() * BURN_LINES.length)];
      return {
        category: "burn",
        text: fn(name, laps, spoken),
      };
    }
    if (lapEvt.is_fast) {
      return {
        category: "pr",
        text: `${name} is flying! Lap ${laps} done in ${spoken}.`,
      };
    }
    return {
      category: "standard",
      text: `${name}, lap ${laps} in ${spoken}.`,
    };
  }
}

const announcer = new AnnouncerService();

// --- DOM Elements ---
const wsStatusEl = document.getElementById("ws-status");
const sessionBadgeEl = document.getElementById("session-state-badge");
const sessionTimerEl = document.getElementById("session-timer");
const btnStart = document.getElementById("btn-start");
const btnPause = document.getElementById("btn-pause");
const btnReset = document.getElementById("btn-reset");

const cooldownSlider = document.getElementById("cooldown-slider");
const cooldownVal = document.getElementById("cooldown-val");
const burnSlider = document.getElementById("burn-slider");
const burnVal = document.getElementById("burn-val");
const rateSlider = document.getElementById("rate-slider");
const rateVal = document.getElementById("rate-val");
const voiceSelect = document.getElementById("voice-select");
const btnMute = document.getElementById("btn-mute");
const debugToggle = document.getElementById("debug-toggle");
const mirrorToggle = document.getElementById("mirror-toggle");
const simCamToggle = document.getElementById("sim-cam-toggle");
const finishLineSlider = document.getElementById("finish-line-slider");
const finishLineVal = document.getElementById("finish-line-val");

const btnSimUnknown = document.getElementById("btn-sim-unknown");
const btnSimLap = document.getElementById("btn-sim-lap");
const btnSimPack = document.getElementById("btn-sim-pack");
const btnTestTts = document.getElementById("btn-test-tts");

const announcerBadge = document.getElementById("announcer-badge");
const announcerText = document.getElementById("announcer-text");

const leaderboardBody = document.getElementById("leaderboard-body");
const runnerSummary = document.getElementById("runner-summary");
const ignoredBar = document.getElementById("ignored-tags-bar");
const ignoredList = document.getElementById("ignored-tags-list");

// Registration Non-Blocking Popover Dialog
const regDialog = document.getElementById("register-dialog");
const regForm = document.getElementById("register-form");
const regSourceTagInput = document.getElementById("reg-source-tag-id");
const regTagSelect = document.getElementById("reg-tag-select");
const regTagBadge = document.getElementById("reg-tag-badge");
const regQueueBadge = document.getElementById("reg-queue-badge");
const regPortrait = document.getElementById("reg-portrait-preview");
const regNameInput = document.getElementById("reg-runner-name");
const btnSnapPhoto = document.getElementById("btn-snap-photo");
const btnRescanTag = document.getElementById("btn-rescan-tag");
const btnIgnoreTag = document.getElementById("btn-ignore-tag");
const btnRegDismiss = document.getElementById("btn-reg-dismiss");
const btnSaveRunner = document.getElementById("btn-save-runner");

// Edit Runner Modal Dialog
const editDialog = document.getElementById("edit-runner-dialog");
const editForm = document.getElementById("edit-runner-form");
const editTagIdInput = document.getElementById("edit-tag-id");
const editTagSelect = document.getElementById("edit-tag-select");
const editTagBadge = document.getElementById("edit-tag-badge");
const editPortrait = document.getElementById("edit-portrait-preview");
const editNameInput = document.getElementById("edit-runner-name");
const btnEditSnap = document.getElementById("btn-edit-snap");
const btnDeleteRunner = document.getElementById("btn-delete-runner");
const btnEditClose = document.getElementById("btn-edit-close");
const btnEditCancel = document.getElementById("btn-edit-cancel");
const btnEditSave = document.getElementById("btn-edit-save");

// Reset Modal Dialog
const resetDialog = document.getElementById("reset-dialog");
const btnResetClose = document.getElementById("btn-reset-close");

// --- Client State ---
let sessionStatus = "idle";
let baseElapsed = 0;
let baseSyncedPerf = performance.now();
let currentBurnFreq = 25;
let leaderboardCache = [];

const promptQueue = new Map();
let activePromptTagId = null;
let activeRegPreviewB64 = "";
let activeEditPreviewB64 = "";
let deleteArmed = false;

function runnerByTagMap() {
  const m = new Map();
  for (const r of leaderboardCache) {
    m.set(Number(r.tag_id), r);
  }
  return m;
}

function populateTagDropdown(selectEl, selectedTagId, mode = "register", currentTagId = null) {
  const byTag = runnerByTagMap();
  selectEl.innerHTML = "";
  for (let tid = 0; tid < 30; tid += 1) {
    const opt = document.createElement("option");
    opt.value = String(tid);
    const pad = `#${String(tid).padStart(2, "0")}`;
    const owner = byTag.get(tid);

    if (mode === "edit") {
      if (tid === currentTagId) {
        opt.textContent = `${pad} — Current Bib`;
      } else if (owner) {
        opt.textContent = `${pad} — Swap Bibs with ${owner.name}`;
      } else {
        opt.textContent = `${pad} — Move to ${pad} (Available)`;
      }
    } else if (tid === selectedTagId && !owner) {
      opt.textContent = `${pad} — Detected on Camera`;
    } else if (owner) {
      opt.textContent = `${pad} — Update ${owner.name}`;
    } else {
      opt.textContent = `${pad} — Available`;
    }
    selectEl.appendChild(opt);
  }
  selectEl.value = String(selectedTagId);
}

function updateTicker(text, category = "standard") {
  announcerText.textContent = text;
  announcerBadge.className = "speech-badge";
  if (category === "pr" || category === "fast") {
    announcerBadge.classList.add("badge-pr");
    announcerBadge.textContent = "FAST / PR";
  } else if (category === "burn") {
    announcerBadge.classList.add("badge-burn");
    announcerBadge.textContent = "ROAST";
  } else if (category === "welcome") {
    announcerBadge.classList.add("badge-welcome");
    announcerBadge.textContent = "LOCKED IN";
  } else {
    announcerBadge.classList.add("badge-neutral");
    announcerBadge.textContent = "SPLIT";
  }
}

// --- Non-blocking Registration Popover Helpers ---
function openRegPopover() {
  if (supportsNativePopover && typeof regDialog.showPopover === "function") {
    if (!regDialog.matches(":popover-open")) {
      regDialog.showPopover();
    }
  } else if (!regDialog.open) {
    regDialog.show();
  }
}

function closeRegPopover() {
  if (supportsNativePopover && typeof regDialog.hidePopover === "function") {
    if (regDialog.matches(":popover-open")) {
      regDialog.hidePopover();
    }
  } else if (regDialog.open) {
    regDialog.close();
  }
}

function syncPromptModal() {
  if (promptQueue.size === 0) {
    activePromptTagId = null;
    activeRegPreviewB64 = "";
    closeRegPopover();
    return;
  }

  if (activePromptTagId === null || !promptQueue.has(activePromptTagId)) {
    const first = promptQueue.values().next().value;
    activePromptTagId = first.tag_id;
    activeRegPreviewB64 = first.preview_image_base64 || "";
    regSourceTagInput.value = String(first.tag_id);
    populateTagDropdown(regTagSelect, first.tag_id, "register");
    regTagBadge.textContent = `TAG #${String(first.tag_id).padStart(2, "0")}`;
    if (activeRegPreviewB64) {
      regPortrait.src = activeRegPreviewB64;
    }
    regNameInput.value = "";
    regNameInput.setCustomValidity("");
    openRegPopover();
    requestAnimationFrame(() => regNameInput.focus());
  }

  if (promptQueue.size > 1) {
    const keys = [...promptQueue.keys()];
    const pos = keys.indexOf(activePromptTagId) + 1;
    regQueueBadge.textContent = `${pos} of ${promptQueue.size} waiting`;
    regQueueBadge.hidden = false;
  } else {
    regQueueBadge.hidden = true;
  }
}

function enqueueRegisterPrompt(prompt) {
  const tid = Number(prompt.tag_id);
  promptQueue.set(tid, prompt);
  if (activePromptTagId === tid && prompt.preview_image_base64) {
    activeRegPreviewB64 = prompt.preview_image_base64;
    regPortrait.src = activeRegPreviewB64;
  }
  syncPromptModal();
}

// --- Render Leaderboard ---
function renderLeaderboard(runners, ignoredTags = []) {
  leaderboardCache = runners || [];
  const totalLaps = leaderboardCache.reduce((acc, r) => acc + (r.laps || 0), 0);
  runnerSummary.textContent = `${leaderboardCache.length} registered runner${
    leaderboardCache.length === 1 ? "" : "s"
  } · ${totalLaps} total lap${totalLaps === 1 ? "" : "s"}`;

  if (ignoredTags && ignoredTags.length > 0) {
    ignoredBar.hidden = false;
    ignoredList.innerHTML = "";
    for (const tid of ignoredTags) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-mini";
      btn.textContent = `Restore #${String(tid).padStart(2, "0")}`;
      btn.addEventListener("click", () => apiPost("/api/unignore_tag", { tag_id: tid }));
      ignoredList.appendChild(btn);
    }
  } else {
    ignoredBar.hidden = true;
  }

  leaderboardBody.innerHTML = "";
  if (leaderboardCache.length === 0) {
    const tr = document.createElement("tr");
    tr.className = "empty-row";
    const td = document.createElement("td");
    td.colSpan = 7;
    td.textContent =
      "No runners registered yet. Wave a tag16h5 bib in front of the camera or press Simulate Unknown Tag [U].";
    tr.appendChild(td);
    leaderboardBody.appendChild(tr);
    return;
  }

  for (const r of leaderboardCache) {
    const tr = document.createElement("tr");

    // 1. Rank
    const tdRank = document.createElement("td");
    const rankSpan = document.createElement("span");
    rankSpan.className = `rank-badge ${r.rank <= 3 && r.laps > 0 ? `rank-${r.rank}` : ""}`;
    rankSpan.textContent = `#${r.rank}`;
    tdRank.appendChild(rankSpan);
    tr.appendChild(tdRank);

    // 2. Sticky row header: Runner Avatar + Name + Tag Badge
    const thRunner = document.createElement("th");
    thRunner.scope = "row";
    const cellWrap = document.createElement("div");
    cellWrap.className = "runner-cell";

    const img = document.createElement("img");
    img.className = "runner-avatar";
    img.width = 48;
    img.height = 48;
    img.src = r.avatar_url || `/faces/${r.tag_id}.png`;
    img.alt = `${r.name} portrait`;
    cellWrap.appendChild(img);

    const meta = document.createElement("div");
    meta.className = "runner-meta";
    const nameEl = document.createElement("span");
    nameEl.className = "runner-name";
    nameEl.textContent = r.name;
    meta.appendChild(nameEl);

    const badges = document.createElement("div");
    badges.className = "runner-badges";
    const tagBadge = document.createElement("span");
    tagBadge.className = "tag-pill";
    tagBadge.textContent = `TAG #${String(r.tag_id).padStart(2, "0")}`;
    badges.appendChild(tagBadge);

    if (r.cooldown_remaining > 0) {
      const cd = document.createElement("span");
      cd.className = "cooldown-chip";
      cd.textContent = `CD ${Math.ceil(r.cooldown_remaining)}s`;
      badges.appendChild(cd);
    }
    meta.appendChild(badges);
    cellWrap.appendChild(meta);
    thRunner.appendChild(cellWrap);
    tr.appendChild(thRunner);

    // 3. Big Lap Counter
    const tdLaps = document.createElement("td");
    const lapBig = document.createElement("span");
    lapBig.className = "lap-count-big";
    lapBig.textContent = String(r.laps);
    tdLaps.appendChild(lapBig);
    tr.appendChild(tdLaps);

    // 4. Last Lap
    const tdLast = document.createElement("td");
    const lastVal = document.createElement("span");
    lastVal.textContent = r.last_lap_mmss;
    if (r.last_lap_is_pr) {
      lastVal.className = "split-pr";
      const badge = document.createElement("span");
      badge.className = "split-tag split-tag-pr";
      badge.textContent = "PR";
      lastVal.appendChild(badge);
    } else if (r.last_lap_is_slow) {
      lastVal.className = "split-slow";
      const badge = document.createElement("span");
      badge.className = "split-tag split-tag-slow";
      badge.textContent = "SLOW";
      lastVal.appendChild(badge);
    }
    tdLast.appendChild(lastVal);
    tr.appendChild(tdLast);

    // 5. Best Lap
    const tdBest = document.createElement("td");
    tdBest.textContent = r.best_lap_mmss;
    tr.appendChild(tdBest);

    // 6. Avg & Total Time
    const tdAvg = document.createElement("td");
    const avgWrap = document.createElement("div");
    avgWrap.className = "avg-total-cell";
    const avgLine = document.createElement("span");
    avgLine.textContent = `Avg ${r.avg_lap_mmss}`;
    const totLine = document.createElement("span");
    totLine.textContent = `Total ${r.total_mmss}`;
    avgWrap.append(avgLine, totLine);
    tdAvg.appendChild(avgWrap);
    tr.appendChild(tdAvg);

    // 7. Controls (+1 / -1 / Edit Bib & Photo)
    const tdActions = document.createElement("td");
    const actRow = document.createElement("div");
    actRow.className = "row-actions";

    const btnPlus = document.createElement("button");
    btnPlus.type = "button";
    btnPlus.className = "btn-mini";
    btnPlus.setAttribute("aria-label", `Add 1 lap for ${r.name}`);
    btnPlus.textContent = "+1";
    btnPlus.addEventListener("click", () =>
      apiPost(`/api/runners/${r.tag_id}/lap_adjust`, { delta: 1 })
    );

    const btnMinus = document.createElement("button");
    btnMinus.type = "button";
    btnMinus.className = "btn-mini";
    btnMinus.setAttribute("aria-label", `Subtract 1 lap for ${r.name}`);
    btnMinus.textContent = "-1";
    btnMinus.disabled = r.laps === 0;
    btnMinus.addEventListener("click", () =>
      apiPost(`/api/runners/${r.tag_id}/lap_adjust`, { delta: -1 })
    );

    const btnEdit = document.createElement("button");
    btnEdit.type = "button";
    btnEdit.className = "btn-mini";
    btnEdit.setAttribute("aria-label", `Edit name, bib tag, or photo for ${r.name}`);
    btnEdit.textContent = "Edit / Tag";
    btnEdit.addEventListener("click", () => openEditModal(r));

    actRow.append(btnPlus, btnMinus, btnEdit);
    tdActions.appendChild(actRow);
    tr.appendChild(tdActions);

    leaderboardBody.appendChild(tr);
  }
}

// --- Sync State from Server ---
function applyServerState(state) {
  if (state.session) {
    sessionStatus = state.session.status;
    baseElapsed = Number(state.session.elapsed_seconds) || 0;
    baseSyncedPerf = performance.now();

    sessionBadgeEl.textContent = sessionStatus.toUpperCase();
    sessionBadgeEl.className = `clock-label ${sessionStatus}`;
    btnStart.hidden = sessionStatus === "running";
    btnStart.textContent = sessionStatus === "paused" ? "Resume" : "Start";
    btnPause.hidden = sessionStatus !== "running";
    sessionTimerEl.textContent = formatClockMmss(baseElapsed);
  }

  if (state.config) {
    const cfg = state.config;
    if (document.activeElement !== cooldownSlider) {
      cooldownSlider.value = String(Math.round(cfg.cooldown_seconds));
      cooldownVal.textContent = `${Math.round(cfg.cooldown_seconds)}s`;
    }
    if (document.activeElement !== burnSlider) {
      currentBurnFreq = Number(cfg.burn_frequency);
      burnSlider.value = String(currentBurnFreq);
      burnVal.textContent = `${currentBurnFreq}%`;
    }
    if (document.activeElement !== finishLineSlider) {
      const pct = Math.round(cfg.finish_line_x * 100);
      finishLineSlider.value = String(pct);
      finishLineVal.textContent = `${pct}%`;
    }
    debugToggle.checked = Boolean(cfg.debug_overlay);
    mirrorToggle.checked = Boolean(cfg.mirror);
    simCamToggle.checked = Boolean(cfg.simulated_mode);
  }

  if (Array.isArray(state.leaderboard)) {
    renderLeaderboard(state.leaderboard, state.ignored_tags || []);
  }

  if (Array.isArray(state.pending_prompts)) {
    const serverPromptIds = new Set(state.pending_prompts.map((p) => Number(p.tag_id)));
    for (const key of [...promptQueue.keys()]) {
      if (!serverPromptIds.has(key)) promptQueue.delete(key);
    }
    for (const p of state.pending_prompts) {
      if (!promptQueue.has(Number(p.tag_id))) {
        promptQueue.set(Number(p.tag_id), p);
      }
    }
    syncPromptModal();
  }
}

setInterval(() => {
  if (sessionStatus === "running") {
    const dt = (performance.now() - baseSyncedPerf) / 1000;
    sessionTimerEl.textContent = formatClockMmss(baseElapsed + dt);
  }
}, 100);

// --- HTTP Helpers ---
async function apiPost(url, body = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "Request failed");
  }
  return res.json();
}

async function apiDelete(url) {
  const res = await fetch(url, { method: "DELETE" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "Delete failed");
  }
  return res.json();
}

// --- WebSocket Connection ---
let socket = null;
function connectWebSocket() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${proto}//${window.location.host}/ws`);

  socket.addEventListener("open", () => {
    wsStatusEl.textContent = "LIVE";
    wsStatusEl.className = "status-pill status-live";
  });

  socket.addEventListener("message", (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (msg.type === "STATE_SYNC") {
      applyServerState(msg);
    } else if (msg.type === "REGISTER_PROMPT") {
      enqueueRegisterPrompt(msg);
    } else if (msg.type === "PHOTO_UPDATED") {
      const tid = Number(msg.tag_id);
      if (activePromptTagId === tid && msg.preview_image_base64) {
        activeRegPreviewB64 = msg.preview_image_base64;
        regPortrait.src = activeRegPreviewB64;
      }
      if (editDialog.open && Number(editTagIdInput.value) === tid && msg.preview_image_base64) {
        activeEditPreviewB64 = msg.preview_image_base64;
        editPortrait.src = activeEditPreviewB64;
      }
    } else if (msg.type === "RUNNER_REGISTERED") {
      if (msg.is_new && msg.speech_text) {
        announcer.enqueue(msg.speech_text, "welcome");
      }
    } else if (msg.type === "LAP_EVENT") {
      announcer.enqueueLapEvent(msg, currentBurnFreq);
    }
  });

  socket.addEventListener("close", () => {
    wsStatusEl.textContent = "Reconnecting…";
    wsStatusEl.className = "status-pill status-connecting";
    setTimeout(connectWebSocket, 1500);
  });
}

// --- Event Listeners ---
announcer.initVoices(voiceSelect);
voiceSelect.addEventListener("change", () => {
  announcer.selectedVoiceURI = voiceSelect.value;
});

btnMute.addEventListener("click", () => {
  const nextMuted = !announcer.muted;
  announcer.setMuted(nextMuted);
  btnMute.setAttribute("aria-pressed", String(nextMuted));
  btnMute.textContent = nextMuted ? "Audio: MUTED" : "Audio: ON";
});

rateSlider.addEventListener("input", () => {
  const r = Number(rateSlider.value);
  announcer.rate = r;
  rateVal.textContent = `${r.toFixed(2)}×`;
});

cooldownSlider.addEventListener("input", () => {
  cooldownVal.textContent = `${cooldownSlider.value}s`;
});
cooldownSlider.addEventListener("change", () => {
  apiPost("/api/config", { cooldown_seconds: Number(cooldownSlider.value) });
});

burnSlider.addEventListener("input", () => {
  currentBurnFreq = Number(burnSlider.value);
  burnVal.textContent = `${currentBurnFreq}%`;
});
burnSlider.addEventListener("change", () => {
  apiPost("/api/config", { burn_frequency: Number(burnSlider.value) });
});

finishLineSlider.addEventListener("input", () => {
  finishLineVal.textContent = `${finishLineSlider.value}%`;
});
finishLineSlider.addEventListener("change", () => {
  apiPost("/api/config", { finish_line_x: Number(finishLineSlider.value) / 100 });
});

debugToggle.addEventListener("change", () => {
  apiPost("/api/config", { debug_overlay: debugToggle.checked });
});
mirrorToggle.addEventListener("change", () => {
  apiPost("/api/config", { mirror: mirrorToggle.checked });
});
simCamToggle.addEventListener("change", () => {
  apiPost("/api/config", { simulated_mode: simCamToggle.checked });
});

btnStart.addEventListener("click", () => apiPost("/api/session/start"));
btnPause.addEventListener("click", () => apiPost("/api/session/pause"));

btnReset.addEventListener("click", () => {
  resetDialog.returnValue = "";
  resetDialog.showModal();
});
btnResetClose.addEventListener("click", () => resetDialog.close("cancel"));
resetDialog.addEventListener("close", () => {
  const choice = resetDialog.returnValue;
  resetDialog.returnValue = "";
  if (choice === "laps_only") {
    apiPost("/api/session/reset", { clear_runners: false });
  } else if (choice === "full_wipe") {
    apiPost("/api/session/reset", { clear_runners: true });
  }
});

// Registration Popover Actions (with editable Bib/Tag dropdown & live Re-scan)
attachImeSafeEnterSubmit(regNameInput, regForm);
regNameInput.addEventListener("invalid", () => {
  regNameInput.setCustomValidity("Please enter the runner's name.");
});
regNameInput.addEventListener("input", () => {
  regNameInput.setCustomValidity("");
});

regTagSelect.addEventListener("change", () => {
  const chosen = Number(regTagSelect.value);
  regTagBadge.textContent = `TAG #${String(chosen).padStart(2, "0")}`;
});

btnSnapPhoto.addEventListener("click", async () => {
  const targetTid = Number(regTagSelect.value);
  const res = await apiPost("/api/snap_photo", { tag_id: targetTid });
  if (res.preview_image_base64) {
    activeRegPreviewB64 = res.preview_image_base64;
    regPortrait.src = activeRegPreviewB64;
  }
});

btnRescanTag.addEventListener("click", async () => {
  try {
    const res = await apiPost("/api/rescan_tag");
    populateTagDropdown(regTagSelect, res.tag_id, "register");
    regTagBadge.textContent = `TAG #${String(res.tag_id).padStart(2, "0")}`;
    if (res.preview_image_base64) {
      activeRegPreviewB64 = res.preview_image_base64;
      regPortrait.src = activeRegPreviewB64;
    }
    regNameInput.focus();
  } catch (err) {
    updateTicker(err.message, "burn");
  }
});

regForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!regForm.reportValidity()) {
    regNameInput.focus();
    return;
  }
  const chosenTid = Number(regTagSelect.value);
  const sourceTid = Number(regSourceTagInput.value);
  const name = regNameInput.value.trim();
  btnSaveRunner.disabled = true;
  try {
    await apiPost("/api/register", {
      tag_id: chosenTid,
      source_tag_id: sourceTid,
      name,
      preview_image_base64: activeRegPreviewB64 || null,
    });
    promptQueue.delete(sourceTid);
    promptQueue.delete(chosenTid);
    activePromptTagId = null;
    syncPromptModal();
  } finally {
    btnSaveRunner.disabled = false;
  }
});

btnIgnoreTag.addEventListener("click", async () => {
  if (activePromptTagId === null) return;
  const tid = Number(regTagSelect.value);
  const srcTid = activePromptTagId;
  promptQueue.delete(srcTid);
  promptQueue.delete(tid);
  activePromptTagId = null;
  syncPromptModal();
  await apiPost("/api/ignore_tag", { tag_id: tid });
});

btnRegDismiss.addEventListener("click", async () => {
  if (activePromptTagId === null) return;
  const tid = activePromptTagId;
  promptQueue.delete(tid);
  activePromptTagId = null;
  syncPromptModal();
  await apiPost("/api/dismiss_prompt", { tag_id: tid });
});

// Edit Runner Modal Actions (Name + Move/Swap Tag + Retake Photo + Delete)
function resetDeleteButtonState() {
  deleteArmed = false;
  btnDeleteRunner.textContent = "Delete Runner";
  btnDeleteRunner.className = "btn btn-danger-ghost";
}

function openEditModal(runner) {
  const tid = Number(runner.tag_id);
  editTagIdInput.value = String(tid);
  populateTagDropdown(editTagSelect, tid, "edit", tid);
  editTagBadge.textContent = `TAG #${String(tid).padStart(2, "0")}`;
  editNameInput.value = runner.name;
  activeEditPreviewB64 = "";
  editPortrait.src = runner.avatar_url || `/faces/${tid}.png`;
  resetDeleteButtonState();
  editDialog.returnValue = "";
  editDialog.showModal();
}

editTagSelect.addEventListener("change", () => {
  const nextTid = Number(editTagSelect.value);
  editTagBadge.textContent = `TAG #${String(nextTid).padStart(2, "0")}`;
});

attachImeSafeEnterSubmit(editNameInput, editForm);
btnEditClose.addEventListener("click", () => editDialog.close("cancel"));
btnEditCancel.addEventListener("click", () => editDialog.close("cancel"));
editDialog.addEventListener("close", () => {
  editDialog.returnValue = "";
  resetDeleteButtonState();
});

btnEditSnap.addEventListener("click", async () => {
  const tid = Number(editTagSelect.value);
  const res = await apiPost("/api/snap_photo", { tag_id: tid });
  if (res.preview_image_base64) {
    activeEditPreviewB64 = res.preview_image_base64;
    editPortrait.src = activeEditPreviewB64;
  }
});

btnDeleteRunner.addEventListener("click", async () => {
  if (!deleteArmed) {
    deleteArmed = true;
    btnDeleteRunner.textContent = "Confirm Delete?";
    btnDeleteRunner.className = "btn btn-danger";
    return;
  }
  const oldTid = Number(editTagIdInput.value);
  btnDeleteRunner.disabled = true;
  try {
    await apiDelete(`/api/runners/${oldTid}`);
    editDialog.close("deleted");
  } finally {
    btnDeleteRunner.disabled = false;
    resetDeleteButtonState();
  }
});

editForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!editForm.reportValidity()) {
    editNameInput.focus();
    return;
  }
  const oldTid = Number(editTagIdInput.value);
  const newTid = Number(editTagSelect.value);
  btnEditSave.disabled = true;
  try {
    await apiPost(`/api/runners/${oldTid}/edit`, {
      name: editNameInput.value.trim(),
      new_tag_id: newTid,
      preview_image_base64: activeEditPreviewB64 || null,
    });
    editDialog.close("saved");
  } finally {
    btnEditSave.disabled = false;
  }
});

// Simulation Buttons & Keyboard Hotkeys (U = Unknown, L = Lap, P = Pack, T = Roast, D = Debug)
async function triggerSimUnknown() {
  try {
    await apiPost("/api/simulate/scan", { mode: "unknown" });
  } catch (err) {
    updateTicker(err.message, "burn");
  }
}

async function triggerSimLap() {
  try {
    await apiPost("/api/simulate/scan", { mode: "lap" });
  } catch (err) {
    updateTicker(err.message, "burn");
  }
}

async function triggerSimPack() {
  try {
    await apiPost("/api/simulate/scan", { mode: "pack" });
  } catch (err) {
    updateTicker(err.message, "burn");
  }
}

function triggerTestRoast() {
  const sampleRunner = leaderboardCache[0]?.name || "Coach Taylor";
  const fn = BURN_LINES[Math.floor(Math.random() * BURN_LINES.length)];
  announcer.enqueue(fn(sampleRunner, 4, "1 minute 42.0 seconds"), "burn");
}

btnSimUnknown.addEventListener("click", triggerSimUnknown);
btnSimLap.addEventListener("click", triggerSimLap);
btnSimPack.addEventListener("click", triggerSimPack);
btnTestTts.addEventListener("click", triggerTestRoast);

document.addEventListener("keydown", (e) => {
  const tag = (e.target?.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || e.metaKey || e.ctrlKey || e.altKey) {
    return;
  }
  const k = e.key.toLowerCase();
  if (k === "u") {
    e.preventDefault();
    triggerSimUnknown();
  } else if (k === "l") {
    e.preventDefault();
    triggerSimLap();
  } else if (k === "p") {
    e.preventDefault();
    triggerSimPack();
  } else if (k === "t") {
    e.preventDefault();
    triggerTestRoast();
  } else if (k === "d") {
    e.preventDefault();
    debugToggle.checked = !debugToggle.checked;
    apiPost("/api/config", { debug_overlay: debugToggle.checked });
  }
});

connectWebSocket();
