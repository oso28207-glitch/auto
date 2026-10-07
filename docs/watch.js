/* ═══════════════════════════════════════════════════════════════
   watch.js — مشغل HLS + Autoplay + انتقال تلقائي
   ═══════════════════════════════════════════════════════════════ */

(function () {
    'use strict';

    // عناصر الصفحة
    const v = document.getElementById('v') || document.getElementById('video-player');
    const grid = document.getElementById('grid') || document.getElementById('episodes-grid');
    const cn = document.getElementById('cn') || document.getElementById('countdown-num');
    const count = document.getElementById('count') || document.getElementById('autoplay-countdown');
    const loading = document.getElementById('player-loading');
    const epBadge = document.getElementById('current-ep-badge');
    const epSearch = document.getElementById('ep-search');
    const btnPrev = document.getElementById('btn-prev');
    const btnNext = document.getElementById('btn-next');
    const btnAutoplay = document.getElementById('btn-autoplay');

    if (!v || typeof S === 'undefined' || !Array.isArray(S)) {
        console.warn('watch.js: لا توجد بيانات حلقات');
        return;
    }

    // الحالة
    let hls = null;
    let cur = 0;
    let auto = true;
    let timer = null;
    let autoplayEnabled = true;

    // watched (localStorage)
    const WATCHED_KEY = 'shoof_watched_' + (S[0] && S[0].num ? S[0].num : 'x') + '_' + S.length;
    let watched = new Set();
    try {
        watched = new Set(JSON.parse(localStorage.getItem(WATCHED_KEY) || '[]'));
    } catch (e) {
        watched = new Set();
    }

    function saveWatched() {
        try {
            localStorage.setItem(WATCHED_KEY, JSON.stringify([...watched]));
        } catch (e) {}
    }

    // ═══════════════════════════════════════════════════════════
    // بناء شبكة الحلقات
    // ═══════════════════════════════════════════════════════════
    function buildGrid(filter) {
        if (!grid) return;
        filter = (filter || '').trim();
        grid.innerHTML = '';

        S.forEach(function (ep, i) {
            const num = ep.num || (i + 1);
            if (filter && String(num).indexOf(filter) === -1) return;

            const b = document.createElement('button');
            b.textContent = num;
            b.className = 'ep-btn';
            if (watched.has(num)) b.classList.add('watched');
            if (i === cur) b.classList.add('active');
            b.onclick = function () { play(i); };
            grid.appendChild(b);
        });
    }

    if (epSearch) {
        epSearch.addEventListener('input', function (e) {
            buildGrid(e.target.value);
        });
    }

    // ═══════════════════════════════════════════════════════════
    // تشغيل حلقة
    // ═══════════════════════════════════════════════════════════
    function play(i) {
        if (i < 0 || i >= S.length) return;
        cur = i;

        const ep = S[i];
        const url = ep.url || ep;
        const num = ep.num || (i + 1);

        // تحديث العنوان + URL
        document.title = 'الحلقة ' + num;
        try {
            history.replaceState(null, '', '?ep=' + num);
        } catch (e) {}

        // شارة الحلقة الحالية
        if (epBadge) epBadge.textContent = 'الحلقة ' + num;

        // تسجيل مشاهدة
        watched.add(num);
        saveWatched();

        // تحديث الشبكة
        buildGrid(epSearch ? epSearch.value : '');

        // أزرار السابق/التالي
        if (btnPrev) btnPrev.disabled = (i === 0);
        if (btnNext) btnNext.disabled = (i === S.length - 1);

        // إخفاء العد التنازلي
        cancelCountdown();

        // إظهار التحميل
        if (loading) loading.classList.remove('hidden');

        // إلغاء HLS القديم
        if (hls) {
            try { hls.destroy(); } catch (e) {}
            hls = null;
        }

        // تحميل الفيديو
        if (window.Hls && Hls.isSupported()) {
            hls = new Hls({
                maxBufferLength: 30,
                maxMaxBufferLength: 60,
                enableWorker: true,
                lowLatencyMode: false,
                backBufferLength: 30
            });
            hls.loadSource(url);
            hls.attachMedia(v);

            hls.on(Hls.Events.MANIFEST_PARSED, function () {
                if (loading) loading.classList.add('hidden');
                v.play().catch(function (err) {
                    console.log('autoplay blocked:', err);
                });
            });

            hls.on(Hls.Events.ERROR, function (event, data) {
                if (data && data.fatal) {
                    console.error('HLS fatal error:', data);
                    if (loading) loading.classList.add('hidden');
                }
            });
        } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
            v.src = url;
            v.addEventListener('loadedmetadata', function onMeta() {
                v.removeEventListener('loadedmetadata', onMeta);
                if (loading) loading.classList.add('hidden');
                v.play().catch(function () {});
            });
        } else {
            v.src = url;
            v.addEventListener('loadedmetadata', function onMeta() {
                v.removeEventListener('loadedmetadata', onMeta);
                if (loading) loading.classList.add('hidden');
                v.play().catch(function () {});
            });
        }
    }

    // ═══════════════════════════════════════════════════════════
    // التالي/السابق
    // ═══════════════════════════════════════════════════════════
    function playNext() {
        cancelCountdown();
        if (cur < S.length - 1) {
            play(cur + 1);
        }
    }

    function playPrev() {
        cancelCountdown();
        if (cur > 0) {
            play(cur - 1);
        }
    }

    window.playNext = playNext;
    window.playPrev = playPrev;

    if (btnPrev) btnPrev.addEventListener('click', playPrev);
    if (btnNext) btnNext.addEventListener('click', playNext);

    // ═══════════════════════════════════════════════════════════
    // Autoplay toggle
    // ═══════════════════════════════════════════════════════════
    function toggleAutoplay() {
        autoplayEnabled = !autoplayEnabled;
        if (btnAutoplay) {
            if (autoplayEnabled) {
                btnAutoplay.classList.add('active');
                btnAutoplay.textContent = '🔁 تشغيل تلقائي: مفعّل';
            } else {
                btnAutoplay.classList.remove('active');
                btnAutoplay.textContent = '⏸ تشغيل تلقائي: متوقف';
                cancelCountdown();
            }
        }
    }

    window.toggleAutoplay = toggleAutoplay;
    if (btnAutoplay) btnAutoplay.addEventListener('click', toggleAutoplay);

    // ═══════════════════════════════════════════════════════════
    // العد التنازلي
    // ═══════════════════════════════════════════════════════════
    function startCountdown(seconds) {
        if (!count || !cn) return;
        let s = seconds;
        cn.textContent = s;
        count.classList.remove('hidden');

        if (timer) clearInterval(timer);
        timer = setInterval(function () {
            s--;
            cn.textContent = Math.max(0, s);
            if (s <= 0) {
                cancelCountdown();
                playNext();
            }
        }, 1000);
    }

    function cancelCountdown() {
        if (timer) {
            clearInterval(timer);
            timer = null;
        }
        if (count) count.classList.add('hidden');
    }

    window.cancelAutoplay = cancelCountdown;

    // ═══════════════════════════════════════════════════════════
    // تتبع التقدم → عرض العد التنازلي
    // ═══════════════════════════════════════════════════════════
    v.addEventListener('timeupdate', function () {
        if (!v.duration) return;
        const remaining = v.duration - v.currentTime;
        if (autoplayEnabled && remaining <= 10 && remaining > 0
            && cur < S.length - 1 && !timer) {
            startCountdown(Math.floor(remaining));
        }
    });

    // عند انتهاء الحلقة
    v.addEventListener('ended', function () {
        if (autoplayEnabled && cur < S.length - 1) {
            playNext();
        }
    });

    // ═══════════════════════════════════════════════════════════
    // التهيئة — ابدأ من حلقة معينة أو الأولى
    // ═══════════════════════════════════════════════════════════
    function init() {
        if (!S || !S.length) {
            console.warn('watch.js: لا حلقات');
            return;
        }

        // ?ep=XX في الرابط
        let startIdx = 0;
        try {
            const params = new URLSearchParams(location.search);
            const epParam = parseInt(params.get('ep'), 10);
            if (epParam) {
                const idx = S.findIndex(function (e) { return (e.num || 0) === epParam; });
                if (idx >= 0) startIdx = idx;
            } else {
                // ابدأ من أول حلقة غير مشاهدة
                for (let i = 0; i < S.length; i++) {
                    if (!watched.has(S[i].num)) {
                        startIdx = i;
                        break;
                    }
                }
            }
        } catch (e) {}

        buildGrid();
        play(startIdx);
    }

    // انتظر Hls.js إن لم يكن محمّل بعد
    if (typeof Hls === 'undefined') {
        let tries = 0;
        const iv = setInterval(function () {
            tries++;
            if (typeof Hls !== 'undefined' || tries > 20) {
                clearInterval(iv);
                init();
            }
        }, 200);
    } else {
        init();
    }

    console.log('watch.js loaded — ' + S.length + ' حلقة');
})();