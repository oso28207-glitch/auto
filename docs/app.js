// app.js — مشغل HLS + autoplay + انتقال تلقائي
let hls = null;
let currentSeries = null;
let currentEpisode = null;
let countdownTimer = null;

async function loadData() {
    const r = await fetch('series.json');
    return r.json();
}

async function openPlayer(seriesName, episodeNum = 1) {
    const data = await loadData();
    const series = data.series.find(s => s.name === seriesName);
    if (!series) return;

    currentSeries = series;
    currentEpisode = episodeNum;

    document.getElementById('player-modal').classList.remove('hidden');
    document.getElementById('player-title').textContent = series.name;
    document.getElementById('player-subtitle').textContent =
        `الحلقة ${episodeNum} من ${series.episodes || '?'}`;

    buildEpisodesGrid(series);

    const url = series.episodes_urls?.[episodeNum - 1];
    if (!url) return;

    playVideo(url);
}

function playVideo(url) {
    const video = document.getElementById('video-player');

    if (hls) { hls.destroy(); hls = null; }

    if (Hls.isSupported()) {
        hls = new Hls({
            maxBufferLength: 30,
            enableWorker: true,
            lowLatencyMode: false,
        });
        hls.loadSource(url);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
            video.play().catch(e => console.log('autoplay blocked:', e));
        });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = url;
        video.play().catch(() => {});
    }

    // autoplay للانتقال التلقائي
    video.removeEventListener('timeupdate', onTimeUpdate);
    video.removeEventListener('ended', onEnded);
    video.addEventListener('timeupdate', onTimeUpdate);
    video.addEventListener('ended', onEnded);
}

function onTimeUpdate(e) {
    const v = e.target;
    if (!v.duration) return;
    const remaining = v.duration - v.currentTime;
    if (remaining <= 10 && remaining > 0) {
        showCountdown(Math.floor(remaining));
    } else {
        hideCountdown();
    }
}

function onEnded() {
    playNext();
}

function showCountdown(sec) {
    const el = document.getElementById('autoplay-countdown');
    const num = document.getElementById('countdown-num');
    el.classList.remove('hidden');
    num.textContent = sec;
    if (countdownTimer) clearTimeout(countdownTimer);
    countdownTimer = setTimeout(() => playNext(), sec * 1000);
}

function hideCountdown() {
    document.getElementById('autoplay-countdown').classList.add('hidden');
    if (countdownTimer) { clearTimeout(countdownTimer); countdownTimer = null; }
}

function playNext() {
    hideCountdown();
    if (!currentSeries) return;
    const next = currentEpisode + 1;
    const nextUrl = currentSeries.episodes_urls?.[next - 1];
    if (nextUrl) {
        openPlayer(currentSeries.name, next);
    }
}

function buildEpisodesGrid(series) {
    const grid = document.getElementById('episodes-grid');
    grid.innerHTML = '';
    (series.episodes_urls || []).forEach((url, i) => {
        const ep = i + 1;
        const btn = document.createElement('button');
        btn.className = 'ep-btn' + (ep === currentEpisode ? ' active' : '');
        btn.textContent = ep;
        btn.onclick = () => openPlayer(series.name, ep);
        grid.appendChild(btn);
    });
}

function closePlayer() {
    const video = document.getElementById('video-player');
    video.pause();
    if (hls) { hls.destroy(); hls = null; }
    hideCountdown();
    document.getElementById('player-modal').classList.add('hidden');
}

// click handlers
document.addEventListener('click', e => {
    const card = e.target.closest('.card');
    if (card) {
        e.preventDefault();
        const name = decodeURIComponent(card.dataset.series);
        openPlayer(name, 1);
    }
});

// search
document.getElementById('search-input')?.addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll('.card').forEach(c => {
        const name = (c.querySelector('.card-title')?.textContent || '').toLowerCase();
        c.style.display = name.includes(q) ? '' : 'none';
    });
});