/* app.js — سلوك الصفحة الرئيسية */
(function() {
    'use strict';

    // شريط علوي شفاف عند التمرير
    const topbar = document.querySelector('.topbar');
    window.addEventListener('scroll', () => {
        if (window.scrollY > 50) topbar.classList.add('scrolled');
        else topbar.classList.remove('scrolled');
    }, { passive: true });

    // بحث فوري
    const searchInput = document.getElementById('search-input');
    const searchResults = document.getElementById('search-results');
    let seriesData = null;
    let searchTimer = null;

    async function loadSeries() {
        if (seriesData) return seriesData;
        try {
            const r = await fetch('series.json');
            const data = await r.json();
            seriesData = data.series || [];
            return seriesData;
        } catch (e) {
            return [];
        }
    }

    searchInput?.addEventListener('input', (e) => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => doSearch(e.target.value), 180);
    });

    async function doSearch(q) {
        q = (q || '').trim().toLowerCase();
        if (q.length < 2) {
            searchResults.classList.add('hidden');
            return;
        }
        const items = await loadSeries();
        const matches = items.filter(s =>
            s.name.toLowerCase().includes(q)
        ).slice(0, 10);

        if (matches.length === 0) {
            searchResults.innerHTML = '<div class="result" style="color:#999">لا نتائج</div>';
        } else {
            searchResults.innerHTML = matches.map(s => `
                <a class="result" href="watch/${s.slug}.html">
                    <img src="${s.poster}" alt="${s.name}" loading="lazy">
                    <div>
                        <div style="font-weight:600">${s.name}</div>
                        <div style="font-size:12px;color:#999">${s.episodes_count} حلقة • ${s.genre}</div>
                    </div>
                </a>
            `).join('');
        }
        searchResults.classList.remove('hidden');
    }

    // إغلاق النتائج عند النقر خارجها
    document.addEventListener('click', (e) => {
        if (!e.target.closest('.search-box')) {
            searchResults?.classList.add('hidden');
        }
    });

    // تصفية بالتصنيف
    document.querySelectorAll('.main-nav a[data-cat]').forEach(a => {
        a.addEventListener('click', (e) => {
            e.preventDefault();
            const cat = a.dataset.cat;
            document.querySelectorAll('.main-nav a').forEach(x => x.classList.remove('active'));
            a.classList.add('active');

            const rows = document.querySelectorAll('.row');
            if (cat === 'الرئيسية') {
                rows.forEach(r => r.style.display = '');
            } else {
                rows.forEach(r => {
                    r.style.display = r.dataset.category === cat ? '' : 'none';
                });
            }
        });
    });

    // تمرير سلس
    window.scrollToRows = function() {
        document.getElementById('rows-container')?.scrollIntoView({ behavior: 'smooth' });
    };

    // toggle row
    window.toggleRow = function(btn) {
        const row = btn.closest('.row');
        const carousel = row.querySelector('.carousel');
        const expanded = carousel.classList.toggle('expanded');
        btn.textContent = expanded ? 'عرض أقل' : 'عرض الكل';
        carousel.style.maxHeight = expanded ? 'none' : '';
    };

    console.log('Shoof app.js loaded');
})();