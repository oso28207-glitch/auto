/* ═══════════════════════════════════════════════════════════════════════════
   watch.js — مشغل HLS + Autoplay + انتقال تلقائي + دعم المواسم
   ───────────────────────────────────────────────────────────────────────────
   يقرأ من الصفحة:
       window.SEASONS = [{season, eps:[{num,url}]}, ...]   (المصدر الأساسي)
       window.S       = حلقات الموسم الافتراضي            (توافق خلفي)
       window.DEFAULT_SEASON = فهرس الموسم الافتراضي
   ═══════════════════════════════════════════════════════════════════════════ */

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
    const seasonsBar = document.getElementById('seasons-bar');
    const seasonLabel = document.getElementById('season-label');

    // ── بناء قائمة المواسم ──
    let seasons = [];
    if (typeof SEASONS !== 'undefined' && Array.isArray(SEASONS) && SEASONS.length) {
        seasons = SEASONS.map(function (s) {
            return {
                season: (s && s.season) || 1,
                eps: (s && Array.isArray(s.eps)) ? s.eps : []
            };
        });
    } else if (typeof S !== 'undefined' && Array.isArray(S)) {
        seasons = [{ season: 1, eps: S }];
    }

    if (!v || !seasons.length) {
        console.warn('watch.js: لا توجد بيانات حلقات');
        return;
    }

    // الحالة
    let hls = null;
    let curSeason = 0;
    let cur = 0;
    let timer = null;
    let autoplayEnabled = true;

    function curEps() {
        return (seasons[curSeason] && seasons[curSeason].eps) || [];
    }

    function curSeasonNum() {
        return (seasons[curSeason] && seasons[curSeason].season) || 1;
    }

    // watched (localStorage) — مفتاح لكل عمل + لكل موسم
    const WATCHED_KEY = 'shoof_watched_' + location.pathname;
    let watched = new Set();
    try {
        watched = new Set(JSON.parse(localStorage.getItem(WATCHED_KEY) || '[]'));
    } catch (e) {
        watched = new Set();
    }

    function wkey(num) {
        return curSeasonNum() + ':' + num;
    }

    function saveWatched() {
        try {
            localStorage.setItem(WATCHED_KEY, JSON.stringify([...watched]));
        } catch (e) {}
    }

    // رسالة "الحلقة غير متاحة" داخل المشغل
    function showUnavailable() {
        let ov = document.getElementById('ep-unavailable');
        if (!ov) {
            ov = document.createElement('div');
            ov.id = 'ep-unavailable';
            ov.className = 'player-unavailable';
            ov.innerHTML = '<div class="pu-icon">\u23f3</div>' +
                '<p>\u0647\u0630\u0647 \u0627\u0644\u062d\u0644\u0642\u0629 \u0644\u0645 \u062a\u064f\u0631\u0641\u0639 \u0628\u0639\u062f \u0625\u0644\u0649 \u0627\u0644\u0642\u0646\u0627\u0629.</p>' +
                '<a class="pu-btn" href="' + (window.C || '#') + '" target="_blank" rel="noopener">\ud83d\udce1 \u062a\u0627\u0628\u0639 \u0639\u0644\u0649 \u062a\u064a\u0644\u064a\u062c\u0631\u0627\u0645</a>';
            const wrap = (v && v.parentElement) ? v.parentElement : document.body;
            wrap.appendChild(ov);
        }
        ov.classList.remove('hidden');
    }

    function hideUnavailable() {
        const ov = document.getElementById('ep-unavailable');
        if (ov) ov.classList.add('hidden');
    }

    // ═══════════════════════════════════════════════════════════════════════
    // شريط المواسم
    // ═══════════════════════════════════════════════════════════════════════
    function buildSeasonsBar() {
        if (!seasonsBar) return;
        seasonsBar.innerHTML = '';

        if (seasons.length <= 1) {
            seasonsBar.classList.add('hidden');
            if (seasonLabel) seasonLabel.textContent = '';
            return;
        }
        seasonsBar.classList.remove('hidden');

        seasons.forEach(function (s, i) {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'season-btn' + (i === curSeason ? ' active' : '');
            b.innerHTML = '\u0627\u0644\u0645\u0648\u0633\u0645 ' + s.season +
                ' <span class="sc">' + (s.eps ? s.eps.length : 0) + '</span>';
            b.onclick = function () { selectSeason(i); };
            seasonsBar.appendChild(b);
        });

        if (seasonLabel) {
            seasonLabel.textContent = '\u2014 \u0627\u0644\u0645\u0648\u0633\u0645 ' + curSeasonNum();
        }
    }

    function selectSeason(i) {
        if (i < 0 || i >= seasons.length) return;
        if (i === curSeason) return;
        curSeason = i;
        cur = 0;
        buildSeasonsBar();
        buildGrid(epSearch ? epSearch.value : '');

        // ابدأ من أول حلقة غير مشاهدة في الموسم المختار
        const eps = curEps();
        let start = 0;
        for (let k = 0; k < eps.length; k++) {
            if (!watched.has(wkey(eps[k].num))) { start = k; break; }
        }
        play(start);
    }

    // ═══════════════════════════════════════════════════════════════════════
    // بناء شبكة الحلقات (للموسم الحالي)
    // ═══════════════════════════════════════════════════════════════════════
    function buildGrid(filter) {
        if (!grid) return;
        filter = (filter || '').trim();
        grid.innerHTML = '';

        const eps = curEps();
        eps.forEach(function (ep, i) {
            const num = ep.num || (i + 1);
            if (filter && String(num).indexOf(filter) === -1) return;

            const b = document.createElement('button');
            b.textContent = num;
            b.className = 'ep-btn';
            if (watched.has(wkey(num))) b.classList.add('watched');
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

    // ═══════════════════════════════════════════════════════════════════════
    // تشغيل حلقة
    // ═══════════════════════════════════════════════════════════════════════
    function play(i) {
        const eps = curEps();
        if (i < 0 || i >= eps.length) return;
        cur = i;

        const ep = eps[i];
        const url = (ep && typeof ep === 'object') ? String(ep.url || '') : String(ep || '');
        const num = (ep && typeof ep === 'object' && ep.num) ? ep.num : (i + 1);

        // تحديث العنوان + URL
        document.title = '\u0627\u0644\u062d\u0644\u0642\u0629 ' + num;
        try {
            history.replaceState(null, '', '?s=' + curSeasonNum() + '&ep=' + num);
        } catch (e) {}

        // شارة الحلقة الحالية
        if (epBadge) epBadge.textContent = '\u0627\u0644\u062d\u0644\u0642\u0629 ' + num;

        // تسجيل مشاهدة
        watched.add(wkey(num));
        saveWatched();

        // تحديث الشبكة
        buildGrid(epSearch ? epSearch.value : '');

        // أزرار السابق/التالي (مع مراعاة حدود المواسم)
        const atFirst = (i === 0 && curSeason === 0);
        const atLast = (i === eps.length - 1 && curSeason === seasons.length - 1);
        if (btnPrev) btnPrev.disabled = atFirst;
        if (btnNext) btnNext.disabled = atLast;

        // إخفاء العد التنازلي
        cancelCountdown();

        // إلغاء HLS القديم
        if (hls) {
            try { hls.destroy(); } catch (e) {}
            hls = null;
        }

        // لا يوجد رابط بث لهذه الحلقة → رسالة واضحة بدل خطأ المشغل
        if (!url) {
            try { v.removeAttribute('src'); v.load(); } catch (e) {}
            if (loading) loading.classList.add('hidden');
            showUnavailable();
            return;
        }
        hideUnavailable();

        // إظهار التحميل
        if (loading) loading.classList.remove('hidden');

        // ── تحميل الفيديو ──
        const isHls = /\.m3u8(\?|#|$)/i.test(url);

        function nativePlay() {
            try { v.removeAttribute('src'); v.load(); } catch (e) {}
            v.src = url;
            v.addEventListener('loadedmetadata', function onMeta() {
                v.removeEventListener('loadedmetadata', onMeta);
                if (loading) loading.classList.add('hidden');
                v.play().catch(function () {});
            });
            v.addEventListener('error', function onErr() {
                v.removeEventListener('error', onErr);
                if (loading) loading.classList.add('hidden');
                showUnavailable();
            });
        }

        if (isHls && window.Hls && Hls.isSupported()) {
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
                    try { hls.destroy(); } catch (e) {}
                    hls = null;
                    nativePlay();
                }
            });
        } else {
            nativePlay();
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // التالي/السابق (عبر المواسم أيضاً)
    // ═══════════════════════════════════════════════════════════════════════
    function playNext() {
        cancelCountdown();
        const eps = curEps();
        if (cur < eps.length - 1) {
            play(cur + 1);
        } else if (curSeason < seasons.length - 1) {
            selectSeason(curSeason + 1);
        }
    }

    function playPrev() {
        cancelCountdown();
        if (cur > 0) {
            play(cur - 1);
        } else if (curSeason > 0) {
            curSeason--;
            buildSeasonsBar();
            const eps = curEps();
            play(eps.length - 1);
        }
    }

    window.playNext = playNext;
    window.playPrev = playPrev;

    if (btnPrev) btnPrev.addEventListener('click', playPrev);
    if (btnNext) btnNext.addEventListener('click', playNext);

    // ═══════════════════════════════════════════════════════════════════════
    // Autoplay toggle
    // ═══════════════════════════════════════════════════════════════════════
    function toggleAutoplay() {
        autoplayEnabled = !autoplayEnabled;
        if (btnAutoplay) {
            if (autoplayEnabled) {
                btnAutoplay.classList.add('active');
                btnAutoplay.textContent = '\ud83d\udd01 \u062a\u0634\u063a\u064a\u0644 \u062a\u0644\u0642\u0627\u0626\u064a: \u0645\u0641\u0639\u0651\u0644';
            } else {
                btnAutoplay.classList.remove('active');
                btnAutoplay.textContent = '\u23f8 \u062a\u0634\u063a\u064a\u0644 \u062a\u0644\u0642\u0627\u0626\u064a: \u0645\u062a\u0648\u0642\u0641';
                cancelCountdown();
            }
        }
    }

    window.toggleAutoplay = toggleAutoplay;
    if (btnAutoplay) btnAutoplay.addEventListener('click', toggleAutoplay);

    // ═══════════════════════════════════════════════════════════════════════
    // العد التنازلي
    // ═══════════════════════════════════════════════════════════════════════
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

    // ═══════════════════════════════════════════════════════════════════════
    // تتبع التقدم → عرض العد التنازلي
    // ═══════════════════════════════════════════════════════════════════════
    v.addEventListener('timeupdate', function () {
        if (!v.duration) return;
        const remaining = v.duration - v.currentTime;
        const eps = curEps();
        const hasNext = (cur < eps.length - 1) || (curSeason < seasons.length - 1);
        if (autoplayEnabled && remaining <= 10 && remaining > 0 && hasNext && !timer) {
            startCountdown(Math.floor(remaining));
        }
    });

    // عند انتهاء الحلقة
    v.addEventListener('ended', function () {
        const eps = curEps();
        const hasNext = (cur < eps.length - 1) || (curSeason < seasons.length - 1);
        if (autoplayEnabled && hasNext) {
            playNext();
        }
    });

    // ═══════════════════════════════════════════════════════════════════════
    // التهيئة — ابدأ من موسم/حلقة محددة أو الأولى
    // ═══════════════════════════════════════════════════════════════════════
    function init() {
        if (!seasons.length) {
            console.warn('watch.js: لا حلقات');
            return;
        }

        let startSeason = (typeof DEFAULT_SEASON === 'number' && DEFAULT_SEASON >= 0
            && DEFAULT_SEASON < seasons.length) ? DEFAULT_SEASON : 0;
        let startEpNum = null;

        try {
            const params = new URLSearchParams(location.search);
            const sp = parseInt(params.get('s'), 10);
            if (!isNaN(sp)) {
                const idx = seasons.findIndex(function (x) { return x.season === sp; });
                if (idx >= 0) startSeason = idx;
            }
            const epParam = parseInt(params.get('ep'), 10);
            if (!isNaN(epParam)) startEpNum = epParam;
        } catch (e) {}

        curSeason = startSeason;
        buildSeasonsBar();
        buildGrid();

        const eps = curEps();
        let startIdx = 0;

        if (startEpNum !== null) {
            let found = eps.findIndex(function (e) { return (e.num || 0) === startEpNum; });
            if (found < 0) {
                // ابحث في باقي المواسم
                for (let si = 0; si < seasons.length; si++) {
                    const k = seasons[si].eps.findIndex(function (e) {
                        return (e.num || 0) === startEpNum;
                    });
                    if (k >= 0) {
                        curSeason = si;
                        buildSeasonsBar();
                        buildGrid();
                        found = k;
                        break;
                    }
                }
            }
            if (found >= 0) startIdx = found;
        } else {
            // ابدأ من أول حلقة غير مشاهدة في الموسم الحالي
            for (let i = 0; i < eps.length; i++) {
                if (!watched.has(wkey(eps[i].num))) { startIdx = i; break; }
            }
        }

        play(startIdx);
    }

    // انتظر Hls.js إن لم يكن محمّلاً بعد
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

    console.log('watch.js loaded — ' + seasons.length + ' موسم');
})();
