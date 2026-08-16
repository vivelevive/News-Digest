// News Digest dashboard renderer.
// Reads ./data/digest.json (written daily by the GitHub Actions job) and renders
// mobile-first, topic-grouped cards. No frameworks, no external requests.

const DATA_URL = "data/digest.json";

const REGION_FLAG = {
  US: "🇺🇸",
  Asia: "🌏",
  Europe: "🇪🇺",
  Australia: "🇦🇺",
};

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffMs = Date.now() - then;
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatGeneratedAt(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  try {
    return d.toLocaleString("en-AU", {
      timeZone: "Australia/Sydney",
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    }) + " AEST/AEDT";
  } catch {
    return d.toLocaleString();
  }
}

function cardHtml(item) {
  const region = item.region ? `<span class="region-tag">${REGION_FLAG[item.region] ?? ""} ${escapeHtml(item.region)}</span>` : "";
  const paywall = item.paywalled
    ? `<div class="paywall-note">🔒 Paywalled at source — headline &amp; snippet only. Opens in your subscribed app/browser.</div>`
    : "";
  const why = item.why_it_matters
    ? `<div class="why"><b>Why it matters:</b> ${escapeHtml(item.why_it_matters)}</div>`
    : "";
  const summary = item.summary
    ? `<p class="summary">${escapeHtml(item.summary)}</p>`
    : "";
  return `
    <article class="card">
      <div class="src-row">
        <span>${escapeHtml(item.source)} · ${timeAgo(item.published)}</span>
        ${region}
      </div>
      <h3><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a></h3>
      ${summary}
      ${why}
      <a class="read-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Read full article →</a>
      ${paywall}
    </article>`;
}

function sectionHtml(cat) {
  const items = cat.items || [];
  const body = items.length
    ? `<div class="card-list">${items.map(cardHtml).join("")}</div>`
    : `<div class="empty-topic">No fresh stories today.</div>`;
  const sub = cat.subtitle ? `<span class="topic-sub">${escapeHtml(cat.subtitle)}</span>` : "";

  if (cat.collapsedByDefault) {
    return `
      <details class="low-priority" id="${cat.id}">
        <summary>${escapeHtml(cat.title)}</summary>
        ${body}
      </details>`;
  }

  return `
    <section class="topic" id="${cat.id}">
      <div class="topic-head">
        <h2>${escapeHtml(cat.title)}</h2>
        ${sub}
      </div>
      ${body}
    </section>`;
}

function selectOptionsHtml(categories) {
  return categories
    .map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.navLabel || c.title)}</option>`)
    .join("");
}

function jumpToTopic(id) {
  if (!id) return;
  const el = document.getElementById(id);
  if (!el) return;
  if (el.tagName === "DETAILS") el.open = true;
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function init() {
  const main = document.getElementById("main");
  const select = document.getElementById("topic-select");
  const updated = document.getElementById("updated");
  const banner = document.getElementById("status-banner");

  try {
    const res = await fetch(`${DATA_URL}?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    updated.textContent = `Updated ${formatGeneratedAt(data.generated_at)}`;

    if (Array.isArray(data.warnings) && data.warnings.length) {
      banner.textContent = `⚠ ${data.warnings.join(" · ")}`;
      banner.classList.add("show");
    }

    const categories = data.categories || [];
    select.innerHTML = `<option value="">Topic…</option>${selectOptionsHtml(categories)}`;
    main.innerHTML = categories.map(sectionHtml).join("");
  } catch (err) {
    updated.textContent = "Not yet updated";
    banner.textContent = "⚠ Couldn't load today's digest yet. The first run happens on the next scheduled GitHub Actions job — check back soon, or trigger it manually from the Actions tab.";
    banner.classList.add("show");
    main.innerHTML = `<div class="empty-topic">No digest data found yet.</div>`;
    console.error("Digest load failed:", err);
  }

  select.addEventListener("change", () => {
    const id = select.value;
    jumpToTopic(id);
    select.value = ""; // reset to placeholder so it can be re-selected to jump again
  });
}

init();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  });
}
