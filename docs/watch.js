/* watch.js — مشغل HLS + autoplay + انتقال تلقائي */
(function() {
    'use strict';

    const video = document.getElementById('video-player');
    const loading = document.getElementById('player-loading');
    const countdown = document.getElementById('autoplay-countdown');
    const countdownNum = document.getElementById('countdown-num');
    const epBadge = document.getElementById('current-ep-badge');
    const grid = document.getElementById('episodes-grid');
    const epSearch = document.getElementById('ep-search');
    const btnPrev = document.getElementById('btn-prev');
    const btnNext = document.getElementById('btn-next');
    const btnAutoplay = document.getElementById('btn-autoplay');

    let hls = null;
    let currentEp = 0;
    let autoplayEnabled = true;
    let countdownTimer = null;
    let countdownValue = 10;

    // watched episodes (localStorage)
    const WATCHED_KEY = 'shoof_watched_' + (SERIES?.slug || 'unknown');
    const watched = new Set(JSON.parse(localStorage.getItem(WATCHED_KEY) || '[]'));
    function saveWatched() {
        try { localStorage.setItem(WATCHED_KEY, JSON.stringify([...watched])); } catch(e) {}
    }

    // ═══ بناء شبكة الحلقات ═══
    function buildGrid(filter = '') {
        if (!grid) return;
        grid.innerHTML = '';
        const f = filter.trim();
        SERIES.episodes.forEach((ep, i) => {
            const num = ep.num || (i + 1);
            if (f && !String(num).includes(f)) return;
            const btn = document.createElement('button');
            btn.className = 'ep-btn';
            if (watched.has(num)) btn.classList.add('watched');
            if (i === currentEp) btn.classList.add('active');
            btn.textContent = num;
            btn.onclick = () => playEpisode(i);
            grid.appendChild(btn);
        });
    }

    epSearch?.addEventListener('input', (e) => buildGrid(e.target.value));

    // ═══ تشغيل حلقة ═══
    function playEpisode(index) {
        if (index < 0 || index >= SERIES.episodes.length) return;
        currentEp = index;
        const ep = SERIES.episodes[index];
        const url = ep.url || ep;

        epBadge.textContent = `الحلقة ${ep.num || (index + 1)}`;
        document.title = `${SERIES.name} — الحلقة ${ep.num || (index + 1)}`;
        history.replaceState(null, '', `?ep=${ep.num || (index + 1)}`);

        // تحديث الحالة
        watched.add(ep.num || (index + 1));
        saveWatched();
        buildGrid(epSearch?.value || '');

        // تحديث الأزرار
        btnPrev.disabled = index === 0;
        btnNext.disabled = index === SERIES.episodes.length - 1;

        // تحميل الفيديو
        loading.classList.remove('hidden');
        cancelCountdown();

        if (hls) { hls.destroy(); hls = null; }

        if (window.Hls && Hls.isSupported()) {
            hls = new Hls({
                maxBufferLength: 30,
                maxMaxBufferLength: 60,
                enableWorker: true,
                lowLatencyMode: false,
                backBufferLength: 30,
            });
            hls.loadSource(url);
            hls.attachMedia(video);
            hls.on(Hls.Events.MANIFEST_PARSED, () => {
                loading.classList.add('hidden');
                video.play().catch(e => console.log('autoplay blocked:', e));
            });
            hls.on(Hls.Events.ERROR, (e, data) => {
                if (data.fatal) {
                    console.error('HLS error:', data);
                    loading.classList.add('hidden');
                }
            });
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = url;
            video.addEventListener('loadedmetadata', () => {
                loading.classList.add('hidden');
                video.play().catch(() => {});
            }, { once: true });
        } else {
            // fallback: yt-dlp/ffmpeg سيعالجها
            video.src = url;
            video.addEventListener('loadedmetadata', () => {
                loading.classList.add('hidden');
                video.play().catch(() => {});
            }, { once: true });
        }
    }

    // ═══ التالي/السابق ═══
    window.playNext = function() {
        cancelCountdown();
        if (currentEp < SERIES.episodes.length - 1) {
            playEpisode(currentEp + 1);
        }
    };

    window.playPrev = function() {
        cancelCountdown();
        if (currentEp > 0) playEpisode(currentEp - 1);
    };

    btnPrev?.addEventListener('click', window.playPrev);
    btnNext?.addEventListener('click', window.playNext);

    // ═══ Autoplay toggle ═══
    window.toggleAutoplay = function() {
        autoplayEnabled = !autoplayEnabled;
        if (autoplayEnabled) {
            btnAutoplay.classList.add('active');
            btnAutoplay.textContent = '🔁 تشغيل تلقائي: مفعّل';
        } else {
            btnAutoplay.classList.remove('active');
            btnAutoplay.textContent = '⏸ تشغيل تلقائي: متوقف';
            cancelCountdown();
        }
    };

    // ═══ العد التنازلي ═══
    function startCountdown(seconds) {
        countdownValue = seconds;
        countdownNum.textContent = seconds;
        countdown.classList.remove('hidden');
        countdownTimer = setInterval(() => {
            countdownValue--;
            countdownNum.textContent = Math.max(0, countdownValue);
            if (countdownValue <= 0) {
                cancelCountdown();
                playNext();
            }
        }, 1000);
    }

    function cancelCountdown() {
        if (countdownTimer) {
            clearInterval(countdownTimer);
            countdownTimer = null;
        }
        countdown.classList.add('hidden');
    }

    window.cancelAutoplay = cancelCountdown;

    // ═══ تتبع التقدم ═══
    video?.addEventListener('timeupdate', () => {
        if (!video.duration) return;
        const remaining = video.duration - video.currentTime;
        if (autoplayEnabled && remaining <= 10 && remaining > 0
            && currentEp < SERIES.episodes.length - 1) {
            if (countdown.classList.contains('hidden')) {
                startCountdown(Math.floor(remaining));
            }
        }
    });

    // ═══ عند انتهاء الحلقة ═══
    video?.addEventListener('ended', () => {
        if (autoplayEnabled && currentEp < SERIES.episodes.length - 1) {
            playNext();
        }
    });

    // ═══ تحميل الحلقة الأولى ═══
    function init() {
        if (!SERIES?.episodes?.length) {
            epBadge.textContent = 'لا توجد حلقات';
            return;
        }

        // تحقق من ?ep=X في الرابط
        const params = new URLSearchParams(location.search);
        const epParam = parseInt(params.get('ep'));
        let startIdx = 0;
        if (epParam) {
            const idx = SERIES.episodes.findIndex(e => (e.num || 0) === epParam);
            if (idx >= 0) startIdx = idx;
        } else {
            // ابدأ من آخر حلقة غير مشاهدة
            for (let i = 0; i < SERIES.episodes.length; i++) {
                if (!watched.has(SERIES.episodes[i].num)) {
                    startIdx = i;
                    break;
                }
            }
        }

        buildGrid();
        playEpisode(startIdx);
    }

    init();
    console.log('Shoof watch.js loaded');
})();