/* ═══════════════════════════════════════════════════════════
   شوف — Shoof App
   يقرأ series.json و videos.json ويعرضها
   ═══════════════════════════════════════════════════════════ */

const API_BASE = (window.APP_CONFIG?.API_BASE || "").replace(/\/$/, "");

const state = {
  series: [],
  filtered: [],
  currentSort: "recent",
  search: "",
  page: 1,
  perPage: 30,
};

// ═══ Helpers ═══
const $ = (id) => document.getElementById(id);

function toast(msg, ms = 2000) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

function formatSize(bytes) {
  if (!bytes) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(1)} ${u[i]}`;
}

function formatDate(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return "الآن";
    if (diff < 3600) return `${Math.floor(diff / 60)} دقيقة`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} ساعة`;
    if (diff < 604800) return `${Math.floor(diff / 86400)} يوم`;
    return d.toLocaleDateString("ar-EG");
  } catch { return ""; }
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

// ═══ Rendering ═══
function renderStats() {
  $("statSeries").textContent = state.series.length;
  const totalEps = state.series.reduce((s, x) => s + (x.count || 0), 0);
  $("statEpisodes").textContent = totalEps;
  const dates = state.series.map(s => s.last_updated).filter(Boolean).sort().reverse();
  $("statUpdated").textContent = dates[0] ? formatDate(dates[0]) : "—";
}

function renderSeries(series) {
  const card = document.createElement("div");
  card.className = "card";
  const initial = (series.name || "?").charAt(0);
  const epsCount = series.count || 0;
  const isNew = series.last_updated && (Date.now() - new Date(series.last_updated)) < 7 * 86400000;
  card.innerHTML = `
    <div class="card-thumb">
      <div class="card-thumb-placeholder">${escapeHtml(initial)}</div>
      ${isNew ? '<span class="card-badge new">جديد</span>' : ''}
      <div class="card-play">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
      </div>
    </div>
    <div class="card-body">
      <h3 class="card-title">${escapeHtml(series.name)}</h3>
      <div class="card-sub">${epsCount} حلقة</div>
    </div>
  `;
  card.addEventListener("click", () => openSeries(series));
  return card;
}

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";

  if (!state.filtered.length) {
    $("emptyBox").classList.remove("hidden");
    $("loadMoreWrap").classList.add("hidden");
    return;
  }
  $("emptyBox").classList.add("hidden");

  const start = 0;
  const end = state.page * state.perPage;
  const visible = state.filtered.slice(start, end);

  const frag = document.createDocumentFragment();
  visible.forEach(s => frag.appendChild(renderSeries(s)));
  grid.appendChild(frag);

  if (end < state.filtered.length) {
    $("loadMoreWrap").classList.remove("hidden");
  } else {
    $("loadMoreWrap").classList.add("hidden");
  }
}

function applySort() {
  const arr = [...state.series];

  if (state.search) {
    const q = state.search.toLowerCase();
    state.filtered = arr.filter(s => s.name.toLowerCase().includes(q));
  } else {
    state.filtered = arr;
  }

  switch (state.currentSort) {
    case "recent":
      state.filtered.sort((a, b) => (b.last_updated || "").localeCompare(a.last_updated || ""));
      break;
    case "name":
      state.filtered.sort((a, b) => a.name.localeCompare(b.name, "ar"));
      break;
    case "episodes":
      state.filtered.sort((a, b) => (b.count || 0) - (a.count || 0));
      break;
  }

  state.page = 1;
  renderGrid();
}

// ═══ Series Detail ═══
function openSeries(series) {
  $("seriesTitle").textContent = series.name;
  $("seriesMeta").textContent = `${series.count} حلقة`;

  const grid = $("episodesGrid");
  grid.innerHTML = "";

  if (!series.episodes || !series.episodes.length) {
    grid.innerHTML = '<p style="grid-column:1/-1;text-align:center;color:var(--text-3);padding:20px">لا توجد حلقات</p>';
  } else {
    series.episodes.forEach(ep => {
      const btn = document.createElement("button");
      btn.className = "ep-btn";
      btn.textContent = ep.episode;
      btn.addEventListener("click", () => {
        closeSeries();
        openPlayer(ep, series.name);
      });
      grid.appendChild(btn);
    });
  }

  $("seriesModal").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeSeries() {
  $("seriesModal").classList.add("hidden");
  document.body.style.overflow = "";
}

// ═══ Player ═══
function openPlayer(ep, seriesName) {
  const modal = $("playerModal");
  const video = $("player");
  const loader = $("playerLoader");

  $("modalTitle").textContent = `${seriesName} — الحلقة ${ep.episode}`;
  $("modalDesc").textContent = "";

  const streamUrl = `${API_BASE}/stream?fid=${encodeURIComponent(ep.message_id)}&mid=${ep.message_id}`;

  // ملاحظة: نحتاج file_id و size الفعليين، لكن نجرب أولاً بالـ message_id
  // سيتم تعديله من builder.py ليشمل file_id الحقيقي

  if (!ep.file_id || !ep.size) {
    // نحتاج بيانات إضافية
    video.src = "";
    loader.innerHTML = '<p style="color:var(--text-2)">البيانات غير مكتملة (file_id/size مفقودان)</p>';
    loader.classList.remove("hidden");
  } else {
    video.src = `${API_BASE}/stream?fid=${encodeURIComponent(ep.file_id)}&size=${ep.size}&mid=${ep.message_id}`;
    loader.classList.remove("hidden");
    video.load();
    video.play().catch(() => {});
  }

  video.onloadeddata = () => loader.classList.add("hidden");
  video.onerror = () => {
    loader.innerHTML = '<p style="color:var(--error)">تعذر تشغيل الفيديو</p>';
  };

  modal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closePlayer() {
  const video = $("player");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("playerModal").classList.add("hidden");
  $("playerLoader").classList.add("hidden");
  document.body.style.overflow = "";
}

// ═══ Init ═══
async function init() {
  $("year").textContent = new Date().getFullYear();

  try {
    const res = await fetch("series.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.series = await res.json();
    if (!Array.isArray(state.series)) state.series = [];
  } catch (e) {
    console.error("فشل تحميل series.json:", e);
    $("loading").classList.add("hidden");
    $("errorBox").classList.remove("hidden");
    $("errorMsg").textContent = "تعذر تحميل البيانات. حاول تحديث الصفحة.";
    return;
  }

  $("loading").classList.add("hidden");
  renderStats();
  applySort();

  // Events
  $("search").addEventListener("input", (e) => {
    state.search = e.target.value.trim();
    applySort();
  });

  document.querySelectorAll(".tab").forEach(tab => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      state.currentSort = tab.dataset.sort;
      applySort();
    });
  });

  $("loadMore").addEventListener("click", () => {
    state.page++;
    renderGrid();
  });

  // Scroll top
  const scrollBtn = $("scrollTop");
  window.addEventListener("scroll", () => {
    if (window.scrollY > 400) scrollBtn.classList.remove("hidden");
    else scrollBtn.classList.add("hidden");
  }, { passive: true });

  // Keyboard
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$("playerModal").classList.contains("hidden")) closePlayer();
      else if (!$("seriesModal").classList.contains("hidden")) closeSeries();
    }
  });

  console.log(`✅ شوف: ${state.series.length} مسلسل`);
}

document.addEventListener("DOMContentLoaded", init);