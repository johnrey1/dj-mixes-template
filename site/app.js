(function () {
  const listEl = document.getElementById('mix-list');
  const filtersEl = document.getElementById('mix-filters');
  let currentStopFn = null;
  let allMixes = [];
  let activeFilter = 'all';

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

  function formatDuration(totalSeconds) {
    if (!totalSeconds && totalSeconds !== 0) return '';
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = Math.floor(totalSeconds % 60);
    const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
    const ss = String(s).padStart(2, '0');
    return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
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

  function buildTrack(mix, index) {
    const track = document.createElement('article');
    track.className = 'mix-track';
    track.style.setProperty('--i', String(index));

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

    if (window.WaveSurfer) {
      attachWaveform(body, actions, mix);
    } else {
      attachNativeAudio(body, mix);
    }

    actions.appendChild(buildDownloadLink(mix));
    body.appendChild(actions);

    if (Array.isArray(mix.tracklist) && mix.tracklist.length) {
      body.appendChild(buildTracklist(mix));
    }

    row.appendChild(body);
    track.appendChild(row);

    return track;
  }

  function buildTracklist(mix) {
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
    mix.tracklist.forEach((track) => {
      const li = document.createElement('li');
      li.textContent = track;
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

  function attachWaveform(body, actions, mix) {
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
      attachNativeAudio(body, mix);
      return;
    }

    const stop = () => ws.pause();

    const togglePlay = () => {
      if (ws.isPlaying()) {
        ws.pause();
      } else {
        setCurrentPlayer(stop);
        ws.play();
      }
    };

    waveformEl.addEventListener('click', togglePlay);
    playBtn.addEventListener('click', togglePlay);

    ws.on('play', () => { playBtn.textContent = '❚❚ pause'; });
    ws.on('pause', () => { playBtn.textContent = '▶ play'; });
    ws.on('finish', () => { playBtn.textContent = '▶ play'; });
  }

  function attachNativeAudio(body, mix) {
    const audio = document.createElement('audio');
    audio.className = 'mix-track__audio-fallback';
    audio.controls = true;
    audio.preload = 'none';
    audio.src = mix.audio_url;

    const stop = () => audio.pause();
    audio.addEventListener('play', () => setCurrentPlayer(stop));

    body.appendChild(audio);
  }

  function renderList() {
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
    sorted.forEach((mix, index) => listEl.appendChild(buildTrack(mix, index)));
  }

  fetch('./mixes.json')
    .then((res) => {
      if (!res.ok) throw new Error(`Failed to load mixes.json: ${res.status}`);
      return res.json();
    })
    .then((mixes) => {
      allMixes = mixes;
      buildFilters();
      renderList();
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
