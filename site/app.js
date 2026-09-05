(function () {
  const listEl = document.getElementById('mix-list');
  const filtersEl = document.getElementById('mix-filters');
  let currentStopFn = null;
  let allMixes = [];
  let activeFilter = 'all';
  let autoplayArmed = true;

  // Per-mix playback handles for the currently rendered list — { seek, play }.
  // Keyed by mix.id, rebuilt on every renderList(); lets a tracklist row or a
  // ?t= deep link drive whichever player (waveform or native <audio>) that mix got.
  const controllers = new Map();

  // Generic fallback — used whenever config.js is missing, fails to load/parse,
  // or is missing individual fields. Deliberately NOT this site's actual branding,
  // so a broken config.js never leaks personal branding into a shared/public copy
  // of this code.
  const DEFAULT_CONFIG = {
    siteName: 'DJ Mixes',
    eyebrow: '◉ live archive — signal continuous',
    tagline: 'stream anywhere / no account required',
    footerText: 'shared with friends — no tracking, no ads',
    categories: {
      recording: { pillLabel: 'Mixes', badgeLabel: 'Recording' },
      live: { pillLabel: 'Live', badgeLabel: 'Live' },
      event: { pillLabel: 'Event', badgeLabel: 'Event' },
    },
  };

  function mergeConfig(user, fallback) {
    const cfg = {};
    ['siteName', 'eyebrow', 'tagline', 'footerText'].forEach((field) => {
      const value = user && user[field];
      if (typeof value === 'string' && value) {
        cfg[field] = value;
      } else {
        cfg[field] = fallback[field];
        if (user) console.warn(`[config.js] missing or invalid "${field}" — using default`);
      }
    });

    cfg.categories = {};
    Object.keys(fallback.categories).forEach((key) => {
      const userCat = user && user.categories && user.categories[key];
      const fallbackCat = fallback.categories[key];
      const pillLabel = userCat && typeof userCat.pillLabel === 'string' && userCat.pillLabel;
      const badgeLabel = userCat && typeof userCat.badgeLabel === 'string' && userCat.badgeLabel;
      cfg.categories[key] = {
        pillLabel: pillLabel || fallbackCat.pillLabel,
        badgeLabel: badgeLabel || fallbackCat.badgeLabel,
      };
      if (user && (!pillLabel || !badgeLabel)) {
        console.warn(`[config.js] category "${key}" missing/incomplete — using default label(s)`);
      }
    });

    return cfg;
  }

  const CONFIG = mergeConfig(window.SITE_CONFIG, DEFAULT_CONFIG);

  const FILTERS = [
    { key: 'all', label: 'All' },
    { key: 'recording', label: CONFIG.categories.recording.pillLabel },
    { key: 'live', label: CONFIG.categories.live.pillLabel },
    { key: 'event', label: CONFIG.categories.event.pillLabel },
  ];

  const TYPE_LABELS = {
    recording: CONFIG.categories.recording.badgeLabel,
    live: CONFIG.categories.live.badgeLabel,
    event: CONFIG.categories.event.badgeLabel,
  };

  function applyConfig() {
    document.title = CONFIG.siteName;
    const eyebrowEl = document.getElementById('site-eyebrow');
    const nameEl = document.getElementById('site-name');
    const taglineEl = document.getElementById('site-tagline');
    const footerEl = document.getElementById('site-footer-text');
    if (eyebrowEl) eyebrowEl.textContent = CONFIG.eyebrow;
    if (nameEl) nameEl.textContent = CONFIG.siteName;
    if (taglineEl) taglineEl.textContent = CONFIG.tagline;
    if (footerEl) footerEl.textContent = CONFIG.footerText;
  }

  applyConfig();

  function setCurrentPlayer(stopFn) {
    if (currentStopFn && currentStopFn !== stopFn) {
      currentStopFn();
    }
    currentStopFn = stopFn;
  }

  // Defensive against a play() that throws synchronously instead of
  // rejecting (not the case for WaveSurfer 7.8.6 / modern <audio>, but
  // cheap insurance if the CDN version ever drifts).
  function attemptPlay(fn) {
    try {
      Promise.resolve(fn()).catch(() => {});
    } catch (err) {
      // autoplay blocked or unsupported — ignore, playback stays manual
    }
  }

  function formatDuration(totalSeconds) {
    if (!totalSeconds && totalSeconds !== 0) return '';
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = Math.floor(totalSeconds % 60);
    const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
    const ss = String(s).padStart(2, '0');
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }

  // Accepts an integer number of seconds ("754") or a clock string ("12:34",
  // "1:02:05"). Returns seconds, or null when there's nothing usable. 0 is valid.
  function parseTimeParam(raw) {
    if (raw == null) return null;
    const str = String(raw).trim();
    if (/^\d+$/.test(str)) return parseInt(str, 10);
    const m = str.match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
    if (!m) return null;
    const a = parseInt(m[1], 10);
    const b = parseInt(m[2], 10);
    return m[3] != null ? a * 3600 + b * 60 + parseInt(m[3], 10) : a * 60 + b;
  }

  // Direct link to a mix, optionally to a point in it (?mix=<id>&t=<seconds>).
  function mixLink(mixId, tSeconds) {
    const u = new URL(location.href);
    u.search = '';
    u.hash = '';
    if (mixId) u.searchParams.set('mix', mixId);
    if (tSeconds != null) u.searchParams.set('t', String(Math.max(0, Math.round(tSeconds))));
    return u.toString();
  }

  function copyLink(url, onCopied) {
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      prompt('Copy this link:', url);
      return;
    }
    navigator.clipboard.writeText(url).then(
      () => { if (onCopied) onCopied(); },
      () => prompt('Copy this link:', url),
    );
  }

  function formatDate(dateStr) {
    const d = new Date(dateStr + 'T00:00:00');
    if (isNaN(d)) return dateStr;
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }

  function mixType(mix) {
    return TYPE_LABELS[mix.type] ? mix.type : 'recording';
  }

  function buildFilters() {
    filtersEl.innerHTML = '';
    FILTERS.forEach((filter) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'mix-filter-btn';
      btn.textContent = filter.label;
      btn.setAttribute('aria-pressed', String(filter.key === activeFilter));
      btn.classList.toggle('is-active', filter.key === activeFilter);
      btn.addEventListener('click', () => {
        activeFilter = filter.key;
        buildFilters();
        renderList();
      });
      filtersEl.appendChild(btn);
    });
  }

  function buildTrack(mix, index, autoplay) {
    const track = document.createElement('article');
    track.className = 'mix-track';
    track.style.setProperty('--i', String(index));
    if (mix.id) track.id = 'mixtrack-' + mix.id;

    const row = document.createElement('div');
    row.className = 'mix-track__row';

    const idx = document.createElement('span');
    idx.className = 'mix-track__index';
    idx.textContent = `N°${String(index + 1).padStart(2, '0')}`;
    row.appendChild(idx);

    const body = document.createElement('div');
    body.className = 'mix-track__body';

    const top = document.createElement('div');
    top.className = 'mix-track__top';

    const title = document.createElement('h2');
    title.className = 'mix-track__title';
    title.textContent = mix.title;
    top.appendChild(title);

    const meta = document.createElement('span');
    meta.className = 'mix-track__meta';
    const metaParts = [formatDate(mix.date)];
    if (mix.bpm_range) metaParts.push(`${mix.bpm_range} bpm`);
    if (mix.duration_seconds) metaParts.push(formatDuration(mix.duration_seconds));
    meta.textContent = metaParts.join(' · ');
    top.appendChild(meta);

    body.appendChild(top);

    const badge = document.createElement('span');
    badge.className = `mix-track__badge mix-track__badge--${mixType(mix)}`;
    badge.textContent = TYPE_LABELS[mixType(mix)];
    body.appendChild(badge);

    if (mixType(mix) === 'event' && (mix.artist || mix.event_name)) {
      const byline = document.createElement('p');
      byline.className = 'mix-track__byline';
      const parts = [];
      if (mix.artist) parts.push(`by ${mix.artist}`);
      if (mix.event_name) parts.push(mix.event_name);
      byline.textContent = parts.join(' — ');
      body.appendChild(byline);
    }

    if (mix.notes) {
      const notes = document.createElement('p');
      notes.className = 'mix-track__notes';
      notes.textContent = mix.notes;
      body.appendChild(notes);
    }

    const actions = document.createElement('div');
    actions.className = 'mix-track__actions';

    const controller = window.WaveSurfer
      ? attachWaveform(body, actions, mix, autoplay)
      : attachNativeAudio(body, mix, autoplay);
    if (mix.id && controller) controllers.set(mix.id, controller);

    actions.appendChild(buildShareLink(mix));
    actions.appendChild(buildDownloadLink(mix));
    body.appendChild(actions);

    if (Array.isArray(mix.tracklist) && mix.tracklist.length) {
      body.appendChild(buildTracklist(mix, controller));
    }

    row.appendChild(body);
    track.appendChild(row);

    return track;
  }

  function buildTracklist(mix, controller) {
    const details = document.createElement('details');
    details.className = 'mix-track__tracklist';

    const summary = document.createElement('summary');
    const chevron = document.createElement('span');
    chevron.className = 'chevron';
    chevron.setAttribute('aria-hidden', 'true');
    chevron.textContent = '▸';
    summary.appendChild(chevron);
    summary.appendChild(document.createTextNode(`tracklist (${mix.tracklist.length})`));
    details.appendChild(summary);

    const ol = document.createElement('ol');
    mix.tracklist.forEach((entry) => {
      const item = (entry && typeof entry === 'object') ? entry : { title: String(entry) };
      const hasTime = typeof item.time_seconds === 'number' && isFinite(item.time_seconds);
      const li = document.createElement('li');

      if (hasTime && controller) {
        const clock = formatDuration(item.time_seconds);

        const jump = document.createElement('button');
        jump.type = 'button';
        jump.className = 'mix-track__ts';
        jump.textContent = clock;
        jump.setAttribute('aria-label', `Play ${item.title} from ${clock}`);
        jump.addEventListener('click', () => {
          controller.seek(item.time_seconds);
          controller.play();
        });
        li.appendChild(jump);

        const label = document.createElement('span');
        label.className = 'mix-track__ts-title';
        label.textContent = item.title;
        li.appendChild(label);

        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'mix-track__ts-copy';
        copy.textContent = '⛓';
        copy.setAttribute('aria-label', `Copy link to ${item.title} at ${clock}`);
        let revert = null;
        copy.addEventListener('click', () => {
          copyLink(mixLink(mix.id, item.time_seconds), () => {
            clearTimeout(revert);
            copy.classList.add('is-copied');
            revert = setTimeout(() => copy.classList.remove('is-copied'), 1500);
          });
        });
        li.appendChild(copy);
      } else {
        li.textContent = item.title;
      }

      ol.appendChild(li);
    });
    details.appendChild(ol);

    return details;
  }

  function buildDownloadLink(mix) {
    const link = document.createElement('a');
    link.className = 'mix-track__download';
    link.href = mix.audio_url;
    link.download = '';
    link.textContent = '↓ download';
    return link;
  }

  function buildShareLink(mix) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mix-track__share';
    btn.textContent = '⛓ copy link';
    btn.setAttribute('aria-label', `Copy direct link to ${mix.title}`);

    let revertTimer = null;

    btn.addEventListener('click', () => {
      copyLink(mixLink(mix.id, null), () => {
        clearTimeout(revertTimer);
        btn.textContent = 'copied!';
        revertTimer = setTimeout(() => { btn.textContent = '⛓ copy link'; }, 1500);
      });
    });

    return btn;
  }

  function attachWaveform(body, actions, mix, autoplay) {
    const waveformEl = document.createElement('div');
    waveformEl.className = 'mix-track__waveform';
    body.appendChild(waveformEl);

    const playBtn = document.createElement('button');
    playBtn.type = 'button';
    playBtn.className = 'mix-track__play-btn';
    playBtn.textContent = '▶ play';
    actions.appendChild(playBtn);

    const wsOptions = {
      container: waveformEl,
      waveColor: getComputedStyle(document.documentElement).getPropertyValue('--hairline-strong').trim(),
      progressColor: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
      height: 44,
      cursorColor: 'transparent',
      barWidth: 2,
      barGap: 2,
      barRadius: 1,
      url: mix.audio_url,
    };

    // Pre-computed peaks let WaveSurfer draw the waveform instantly instead of
    // fetching + decoding the whole file client-side (slow for hour-long mixes).
    if (Array.isArray(mix.peaks) && mix.peaks.length && mix.duration_seconds) {
      wsOptions.peaks = [mix.peaks];
      wsOptions.duration = mix.duration_seconds;
    }

    let ws;
    try {
      ws = window.WaveSurfer.create(wsOptions);
    } catch (err) {
      body.removeChild(waveformEl);
      actions.removeChild(playBtn);
      return attachNativeAudio(body, mix, autoplay);
    }

    const clampTime = (t) => {
      const dur = ws.getDuration() || mix.duration_seconds || 0;
      const hi = dur > 0 ? dur - 0.25 : t;
      return Math.min(Math.max(0, t), hi < 0 ? 0 : hi);
    };

    // A seek issued before the media is loaded is dropped by the browser, so
    // hold the request and (re)apply it once WaveSurfer reports a duration —
    // 'ready' is the reliable point. The immediate call is best-effort for the
    // common peaks-provided case where duration is already known.
    let pendingSeek = null;
    const applySeek = () => {
      if (pendingSeek == null || !(ws.getDuration() > 0)) return;
      ws.setTime(clampTime(pendingSeek));
    };
    ws.on('ready', () => { applySeek(); pendingSeek = null; });

    const seek = (t) => {
      if (typeof t !== 'number' || !isFinite(t)) return;
      pendingSeek = t;
      applySeek();
    };

    const stop = () => ws.pause();

    const togglePlay = () => {
      if (ws.isPlaying()) {
        ws.pause();
      } else {
        ws.play();
      }
    };

    waveformEl.addEventListener('click', togglePlay);
    playBtn.addEventListener('click', togglePlay);

    ws.on('play', () => {
      setCurrentPlayer(stop);
      playBtn.textContent = '❚❚ pause';
    });
    ws.on('pause', () => { playBtn.textContent = '▶ play'; });
    ws.on('finish', () => { playBtn.textContent = '▶ play'; });

    if (autoplay) {
      // `once` + the shared armed-flag make this a one-shot: if the user
      // manually starts a different track before this mix's audio finishes
      // loading/decoding, the late `ready` here must not steal playback.
      ws.once('ready', () => {
        if (!autoplayArmed) return;
        autoplayArmed = false;
        attemptPlay(() => ws.play());
      });
    }

    return { seek, play: () => ws.play() };
  }

  function attachNativeAudio(body, mix, autoplay) {
    const audio = document.createElement('audio');
    audio.className = 'mix-track__audio-fallback';
    audio.controls = true;
    audio.preload = 'none';
    audio.src = mix.audio_url;

    const stop = () => audio.pause();
    audio.addEventListener('play', () => setCurrentPlayer(stop));

    body.appendChild(audio);

    if (autoplay && autoplayArmed) {
      autoplayArmed = false;
      attemptPlay(() => audio.play());
    }

    const clampTime = (t) => {
      const dur = audio.duration || mix.duration_seconds || 0;
      const hi = dur > 0 ? dur - 0.25 : t;
      return Math.min(Math.max(0, t), hi < 0 ? 0 : hi);
    };

    // currentTime is ignored until metadata loads; preload='none' means that
    // never happens on its own, so bump preload + load() and finish the seek
    // on 'loadedmetadata'.
    let pendingSeek = null;
    const applySeek = () => {
      if (pendingSeek == null || audio.readyState < 1) return;
      audio.currentTime = clampTime(pendingSeek);
      pendingSeek = null;
    };
    audio.addEventListener('loadedmetadata', applySeek);

    const seek = (t) => {
      if (typeof t !== 'number' || !isFinite(t)) return;
      pendingSeek = t;
      if (audio.readyState >= 1) {
        applySeek();
      } else {
        if (audio.preload === 'none') audio.preload = 'metadata';
        audio.load();
      }
    };

    return { seek, play: () => audio.play() };
  }

  function renderList(targetId) {
    if (currentStopFn) {
      currentStopFn();
      currentStopFn = null;
    }
    controllers.clear();
    listEl.innerHTML = '';

    const filtered = activeFilter === 'all'
      ? allMixes
      : allMixes.filter((mix) => mixType(mix) === activeFilter);

    if (!filtered.length) {
      const empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = allMixes.length
        ? 'no mixes in this category yet'
        : 'no mixes yet — check back soon';
      listEl.appendChild(empty);
      return;
    }

    const sorted = [...filtered].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
    sorted.forEach((mix, index) => listEl.appendChild(buildTrack(mix, index, Boolean(targetId) && mix.id === targetId)));
  }

  fetch('./mixes.json')
    .then((res) => {
      if (!res.ok) throw new Error(`Failed to load mixes.json: ${res.status}`);
      return res.json();
    })
    .then((mixes) => {
      allMixes = Array.isArray(mixes) ? mixes : [];

      const params = new URLSearchParams(location.search);
      const targetId = params.get('mix');
      const startAt = parseTimeParam(params.get('t'));
      const targetMix = targetId && allMixes.find((mix) => mix.id === targetId);
      if (targetMix) activeFilter = 'all';

      buildFilters();
      renderList(targetMix ? targetId : undefined);

      if (targetMix) {
        const el = document.getElementById('mixtrack-' + targetMix.id);
        if (el) {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          el.classList.add('mix-track--highlight');
          setTimeout(() => el.classList.remove('mix-track--highlight'), 2500);
        }

        if (startAt != null) {
          const controller = controllers.get(targetMix.id);
          if (controller) {
            controller.seek(startAt);
            attemptPlay(() => controller.play());
          }
          const tl = el && el.querySelector('.mix-track__tracklist');
          if (tl) tl.open = true;
        }

        const cleanUrl = new URL(location.href);
        cleanUrl.searchParams.delete('mix');
        cleanUrl.searchParams.delete('t');
        history.replaceState(null, '', cleanUrl.pathname + cleanUrl.search + cleanUrl.hash);
      }
    })
    .catch((err) => {
      listEl.innerHTML = '';
      const error = document.createElement('p');
      error.className = 'error';
      error.textContent = 'could not load mixes right now';
      listEl.appendChild(error);
      console.error(err);
    });
})();
