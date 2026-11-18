"use strict";
// LiveCaption web player: HLS.js plays the server's video relay; captions arrive over
// WebSocket with times on the same timeline, so each one is shown at the moment it was spoken.
//   caption on screen while  epoch + start <= hls.playingDate - offset < epoch + end
(() => {
  const $ = (id) => document.getElementById(id);
  const video = $("video"), stage = $("stage"), captionBox = $("caption"), list = $("list");
  const SPEAKER_COLORS = ["#ffffff", "#ffe066", "#7fdbff", "#9be564", "#ff9ecd", "#c9a7ff"];
  const MIN_SHOW_MS = 1800, HOLD_MS = 1200, MAX_CUES = 2000;

  const store = {
    get(key, fallback) { try { return localStorage.getItem("lc." + key) ?? fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem("lc." + key, value); } catch { /* private mode */ } },
  };

  const state = { ws: null, hls: null, session: null, epoch: null, offset: 0, cues: [], byId: new Map(),
                  activeId: null, lastRendered: "", leads: [] };
  window.liveCaptionDebug = state;

  // ---- server address ------------------------------------------------------
  const params = new URLSearchParams(location.search);
  $("server").value = params.get("server") || store.get("server", "") || (location.protocol.startsWith("http") ? location.host : "");
  const serverHost = () => $("server").value.trim().replace(/^\w+:\/\//, "").replace(/\/.*$/, "");
  const httpBase = () => `${location.protocol === "https:" ? "https" : "http"}://${serverHost()}`;
  const wsUrl = () => `${location.protocol === "https:" ? "wss" : "ws"}://${serverHost()}/ws${params.get("token") ? "?token=" + encodeURIComponent(params.get("token")) : ""}`;

  // ---- settings ---------------------------------------------------------------
  $("url").value = params.get("url") || store.get("url", "");
  $("language").value = store.get("language", "ko");
  $("diarization").checked = store.get("diarization", "0") === "1";
  $("margin").value = store.get("margin", "8");
  $("size").value = store.get("size", "28");
  const applySize = () => document.documentElement.style.setProperty("--caption-size", $("size").value + "px");
  const applyMargin = () => { $("margin-label").textContent = $("margin").value + "초"; };
  applySize(); applyMargin();
  $("size").addEventListener("input", () => { applySize(); store.set("size", $("size").value); });
  $("margin").addEventListener("input", () => { applyMargin(); store.set("margin", $("margin").value); });
  $("margin").addEventListener("change", () => { if (state.hls) jumpToLiveMargin(); });

  // ---- status -------------------------------------------------------------------
  function setStatus(stateName, text) {
    $("status").textContent = text;
    $("dot").className = "dot " + ({ running: "running", error: "error", resolving: "busy", connecting: "busy" }[stateName] || "");
  }
  const running = (on) => { $("start").disabled = on; $("stop").disabled = !on; };

  // ---- start / stop --------------------------------------------------------------
  $("start-form").addEventListener("submit", (event) => { event.preventDefault(); start(); });
  $("stop").addEventListener("click", () => stop("정지했습니다."));

  function start() {
    const url = $("url").value.trim();
    if (!url) return;
    stop();
    ["url", "language"].forEach((key) => store.set(key, $(key).value));
    store.set("diarization", $("diarization").checked ? "1" : "0");
    store.set("server", $("server").value.trim());
    clearCues();
    running(true);
    setStatus("connecting", "서버 연결 중…");
    $("placeholder").textContent = "스트림을 준비하는 중… (처음 영상이 나오기까지 10~15초)";
    $("placeholder").hidden = false;
    let ws;
    try { ws = new WebSocket(wsUrl()); } catch (error) { setStatus("error", "서버 주소가 올바르지 않습니다."); running(false); return; }
    state.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "start", url, video: true,
      language: $("language").value || null, diarization: $("diarization").checked, max_speakers: 4 }));
    ws.onmessage = (message) => { if (state.ws === ws) handle(JSON.parse(message.data)); };
    ws.onerror = () => { if (state.ws === ws) setStatus("error", `서버(${serverHost()})에 연결할 수 없습니다.`); };
    ws.onclose = () => { if (state.ws === ws) { state.ws = null; running(false); } };
  }

  function stop(message) {
    if (state.ws) {
      const ws = state.ws;
      state.ws = null;
      try { ws.send(JSON.stringify({ type: "stop" })); } catch { /* already closed */ }
      ws.close();
    }
    if (state.hls) { state.hls.destroy(); state.hls = null; }
    video.removeAttribute("src"); video.load();
    state.epoch = null;
    $("live-badge").hidden = true;
    running(false);
    if (message) setStatus("idle", message);
  }

  // ---- server messages --------------------------------------------------------------
  function handle(message) {
    if (message.type === "status") {
      setStatus(message.state, message.detail || message.state);
      if (["error", "ended", "stopped"].includes(message.state)) running(false);
    } else if (message.type === "session") {
      state.session = message;
      $("title").textContent = message.title || "";
      for (const fmt of ["srt", "vtt"]) {
        const link = $(fmt);
        link.href = `${httpBase()}/sessions/${message.id}.${fmt}`;
        link.classList.remove("disabled");
      }
    } else if (message.type === "timeline") {
      // First video segment exists: caption time 0 is now pinned to the video's clock.
      state.epoch = message.epoch;
      if (state.session && state.session.hls) loadVideo(httpBase() + state.session.hls);
    } else if (message.type === "caption") {
      upsert(message);
    }
  }

  // ---- video ------------------------------------------------------------------------
  function loadVideo(source) {
    const margin = Number($("margin").value);
    if (window.Hls && Hls.isSupported()) {
      // Latency is managed by maintainLatency(); hls.js must not speed up on its own.
      const hls = new Hls({ liveSyncDuration: margin, liveMaxLatencyDuration: margin + 30,
                            maxLiveSyncPlaybackRate: 1, enableWorker: true, lowLatencyMode: false });
      state.hls = hls;
      state.holding = false;
      state.lastHold = 0;
      hls.loadSource(source);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => autoplay());
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else setStatus("error", "영상 재생 오류: " + data.details);
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = source;   // Safari: native HLS (no playingDate, captions follow arrival)
      autoplay();
    } else {
      setStatus("error", "이 브라우저는 HLS 재생을 지원하지 않습니다.");
    }
  }

  async function autoplay() {
    $("placeholder").hidden = true;
    $("live-badge").hidden = false;
    try { await video.play(); }
    catch {
      video.muted = true;  // browsers allow muted autoplay; offer one click for sound
      $("unmute").hidden = false;
      try { await video.play(); } catch { /* user will press play */ }
    }
  }
  $("unmute").addEventListener("click", () => { video.muted = false; $("unmute").hidden = true; });

  function jumpToLiveMargin() {
    if (!state.hls) return;
    state.hls.config.liveSyncDuration = Number($("margin").value);
    state.hls.config.liveMaxLatencyDuration = Number($("margin").value) + 30;
    state.lastHold = 0;
    if (state.hls.liveSyncPosition) video.currentTime = state.hls.liveSyncPosition;
  }

  // Keep the picture "margin" seconds behind the server's newest video, so captions that take
  // a few seconds to recognize still arrive before their scene. Right after start there are
  // only a few seconds of video and hls.js plays at the edge: hold the picture once for a big
  // gap, then nudge the playback rate for small drifts.
  function maintainLatency() {
    if (!state.hls || video.paused || state.holding) return;
    const latency = video.seekable.length ? video.seekable.end(video.seekable.length - 1) - video.currentTime : NaN;
    const margin = Number($("margin").value);
    if (!Number.isFinite(latency)) return;
    state.latency = latency;
    const gap = margin - latency;
    const now = performance.now();
    if (gap >= 2 && now - (state.lastHold || 0) > 15000) {
      state.holding = true;
      state.lastHold = now;
      video.pause();
      setStatus("running", `자막이 먼저 도착하도록 영상을 ${gap.toFixed(1)}초 늦추는 중…`);
      setTimeout(() => {
        state.holding = false;
        video.play().catch(() => {});
        setStatus("running", "음성 인식 중");
      }, gap * 1000);
      return;
    }
    const rate = gap > 0.5 ? 0.9 : gap < -3 ? 1.08 : 1;
    if (video.playbackRate !== rate) video.playbackRate = rate;
  }

  // ---- caption timeline ----------------------------------------------------------------
  function clearCues() {
    state.cues = []; state.byId.clear(); state.activeId = null; state.lastRendered = ""; state.leads = [];
    list.replaceChildren(); captionBox.replaceChildren();
    $("lag").textContent = "–";
    ["srt", "vtt"].forEach((fmt) => $(fmt).classList.add("disabled"));
  }

  function upsert(message) {
    let cue = state.byId.get(message.id);
    if (message.remove) {
      if (cue) {
        state.cues.splice(state.cues.indexOf(cue), 1);
        state.byId.delete(cue.id);
        cue.item.remove();
      }
      return;
    }
    const arrived = performance.now();
    if (!cue) {
      cue = { id: message.id, item: document.createElement("li"), arrived };
      cue.item.addEventListener("click", () => seekTo(cue));
      state.byId.set(cue.id, cue);
      state.cues.push(cue);
      const stick = list.scrollTop + list.clientHeight >= list.scrollHeight - 8;
      list.append(cue.item);
      if (stick) list.scrollTop = list.scrollHeight;
      if (state.cues.length > MAX_CUES) { const old = state.cues.shift(); state.byId.delete(old.id); old.item.remove(); }
    }
    Object.assign(cue, { text: message.text, final: message.final !== false, speaker: speakerName(message.speaker),
                         start: message.stream_start ?? cue.start ?? 0, end: message.stream_end ?? cue.end ?? 0 });
    if (cue.final && typeof message.lag === "number") $("lag").textContent = `약 ${(message.lag / 1000).toFixed(1)}초`;
    // How early the final text arrived before its scene (negative = late). Diagnostics only.
    const position = playheadMs();
    if (cue.final && !cue.lead && position != null) { cue.lead = cue.start - position; state.leads.push(cue.lead); }
    renderItem(cue);
  }

  function speakerName(label) {
    if (!label) return null;
    const match = /^SPEAKER_(\d+)$/.exec(label);
    return match ? `화자 ${Number(match[1]) + 1}` : label;
  }
  function speakerColor(name) {
    if (!name) return SPEAKER_COLORS[0];
    const match = /^화자 (\d+)$/.exec(name);
    const index = match ? Number(match[1]) - 1 : [...name].reduce((sum, ch) => sum + ch.codePointAt(0), 0);
    return SPEAKER_COLORS[index % SPEAKER_COLORS.length];
  }

  function renderItem(cue) {
    const time = document.createElement("time");
    time.textContent = clockText(cue.start);
    const parts = [time];
    if (cue.speaker) {
      const who = document.createElement("span");
      who.className = "who"; who.style.color = speakerColor(cue.speaker); who.textContent = cue.speaker;
      parts.push(who);
    }
    parts.push(document.createTextNode(cue.text + (cue.final ? "" : " …")));
    cue.item.replaceChildren(...parts);
    cue.item.classList.toggle("interim", !cue.final);
  }

  function clockText(ms) {
    const seconds = Math.max(0, Math.floor(ms / 1000));
    return `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  }

  // Where the picture is, in caption time (ms since the first audio sample).
  function playheadMs() {
    const date = state.hls && state.hls.playingDate;
    if (state.epoch == null || !date) return null;
    return date.getTime() - state.epoch - state.offset;
  }

  function activeCue(position) {
    for (let index = state.cues.length - 1; index >= 0; index--) {
      const cue = state.cues[index];
      if (cue.start > position) continue;
      const next = state.cues[index + 1];
      const until = Math.max(cue.end + HOLD_MS, cue.start + MIN_SHOW_MS);
      if (position < until && !(next && next.start <= position)) return cue;
      return null;
    }
    return null;
  }

  function render() {
    const position = playheadMs();
    // Without a shared clock (Safari, or before the first segment) show the newest caption.
    const cue = position == null ? state.cues[state.cues.length - 1] || null : activeCue(position);
    const key = cue ? `${cue.id}|${cue.text}|${cue.final}|${cue.speaker}` : "";
    if (key !== state.lastRendered) {
      state.lastRendered = key;
      captionBox.replaceChildren();
      if (cue) {
        if (cue.speaker && $("diarization").checked) {
          const who = document.createElement("span");
          who.className = "who"; who.textContent = cue.speaker;
          captionBox.append(who);
        }
        captionBox.append(document.createTextNode(cue.text));
        captionBox.style.color = speakerColor(cue.speaker);
        captionBox.classList.toggle("interim", !cue.final);
      }
      if (state.activeId) state.byId.get(state.activeId)?.item.classList.remove("active");
      state.activeId = cue ? cue.id : null;
      cue?.item.classList.add("active");
    }
    if (state.hls) {
      maintainLatency();
      const latency = state.latency;
      $("latency").textContent = Number.isFinite(latency) ? `서버 최신 영상보다 ${latency.toFixed(1)}초 뒤` : "–";
      $("clock").textContent = position == null ? "" : clockText(position);
    }
    requestAnimationFrame(render);
  }
  requestAnimationFrame(render);

  function seekTo(cue) {
    const position = playheadMs();
    if (position == null) return;
    const target = video.currentTime + (cue.start - position) / 1000 - 0.3;
    const range = video.seekable.length ? [video.seekable.start(0), video.seekable.end(video.seekable.length - 1)] : null;
    if (range && target >= range[0] && target <= range[1]) video.currentTime = target;
    else setStatus("running", "그 장면은 이미 영상 버퍼(약 1분) 밖입니다.");
  }

  // ---- offset ---------------------------------------------------------------------------
  function setOffset(ms) {
    state.offset = Math.round(ms);
    $("offset-label").textContent = `${state.offset >= 0 ? "+" : "−"}${Math.abs(state.offset / 1000).toFixed(1)}초`;
    state.lastRendered = "";
  }
  document.querySelectorAll("[data-nudge]").forEach((button) =>
    button.addEventListener("click", () => setOffset(state.offset + Number(button.dataset.nudge))));
  $("offset-reset").addEventListener("click", () => setOffset(0));

  // ---- transport ----------------------------------------------------------------------------
  const togglePlay = () => (video.paused ? video.play() : video.pause());
  $("play").addEventListener("click", togglePlay);
  video.addEventListener("play", () => { $("play").textContent = "❚❚"; });
  video.addEventListener("pause", () => { $("play").textContent = "▶"; });
  $("volume").addEventListener("input", () => { video.volume = Number($("volume").value); video.muted = false; $("unmute").hidden = true; });
  const toggleCaptions = () => { stage.classList.toggle("no-cc"); $("cc").classList.toggle("on", !stage.classList.contains("no-cc")); };
  $("cc").addEventListener("click", toggleCaptions);
  const toggleFullscreen = () => (document.fullscreenElement ? document.exitFullscreen() : stage.requestFullscreen?.());
  $("fullscreen").addEventListener("click", toggleFullscreen);
  video.addEventListener("dblclick", toggleFullscreen);

  let idleTimer = null;
  stage.addEventListener("mousemove", () => {
    stage.classList.remove("idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (!video.paused) stage.classList.add("idle"); }, 2500);
  });

  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, select, textarea") || event.ctrlKey || event.metaKey || event.altKey) return;
    const actions = { " ": togglePlay, f: toggleFullscreen, c: toggleCaptions,
                      g: () => setOffset(state.offset - 100), h: () => setOffset(state.offset + 100) };
    const action = actions[event.key.toLowerCase()];
    if (action) { event.preventDefault(); action(); }
  });
  window.addEventListener("beforeunload", () => stop());
})();
