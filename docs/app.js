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

function entryHtml(item) {
  const region = item.region ? `<span class="region-tag">${REGION_FLAG[item.region] ?? ""} ${escapeHtml(item.region)}</span>` : "";
  const paywall = item.paywalled
    ? `<p class="paywall-note">Paywalled at source — headline and preview only. Opens in your subscribed app or browser.</p>`
    : "";
  // No "Why it matters:" label -- the italic, rust-ruled treatment reads
  // as an editorial aside on its own, the way a margin note does in print.
  const aside = item.why_it_matters
    ? `<p class="aside">${escapeHtml(item.why_it_matters)}</p>`
    : "";
  // No "Summary:" label either -- position under the headline already says
  // what this text is, same as a newspaper deck. Some sources (Google News
  // RSS substitutes, used where a publisher's native RSS was discontinued)
  // don't expose real article text via a plain fetch, so say that plainly
  // instead of silently leaving a gap.
  const dek = item.summary
    ? `<p class="dek">${escapeHtml(item.summary)}</p>`
    : `<p class="dek dek-unavailable">No preview available for this source — tap through to read.</p>`;
  return `
    <article class="entry">
      <div class="byline">
        <span>${escapeHtml(item.source)}, ${timeAgo(item.published)}</span>
        ${region}
      </div>
      <h3><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a></h3>
      ${dek}
      ${aside}
      <a class="read-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Read the full story</a>
      ${paywall}
    </article>`;
}

function sectionHtml(cat) {
  const items = cat.items || [];
  const body = items.length
    ? `<div class="entry-list">${items.map(entryHtml).join("")}</div>`
    : `<p class="empty-topic">No fresh stories today.</p>`;
  const sub = cat.subtitle ? `<span class="section-sub">${escapeHtml(cat.subtitle)}</span>` : "";

  if (cat.collapsedByDefault) {
    return `
      <details class="low-priority" id="${cat.id}">
        <summary>
          <span class="summary-title">${escapeHtml(cat.title)}</span>
          <span class="summary-meta">${cat.subtitle ? escapeHtml(cat.subtitle) : ""}</span>
        </summary>
        ${body}
      </details>`;
  }

  return `
    <section class="topic" id="${cat.id}">
      <div class="section-head">
        <h2>${escapeHtml(cat.title)}</h2>
        ${sub}
      </div>
      ${body}
    </section>`;
}

function briefItemHtml(entry) {
  const paywall = entry.paywalled ? " (paywalled)" : "";
  return `
    <li class="brief-item">
      <a class="brief-cat" href="#${escapeHtml(entry.categoryId)}">${escapeHtml(entry.categoryTitle)}</a>
      <a class="brief-headline" href="${escapeHtml(entry.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(entry.title)}${paywall}</a>
      ${entry.takeaway ? `<p class="brief-takeaway">${escapeHtml(entry.takeaway)}</p>` : ""}
    </li>`;
}

function briefHtml(brief) {
  if (!brief || !brief.length) return "";
  return `
    <section class="brief" id="brief">
      <div class="section-head">
        <h2>Today in brief</h2>
        <span class="section-sub">${brief.length} stories, under 5 minutes</span>
      </div>
      <ol class="brief-list">${brief.map(briefItemHtml).join("")}</ol>
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
      banner.textContent = data.warnings.join(" — ");
      banner.classList.add("show");
    }

    const categories = data.categories || [];
    const briefOption = (data.brief && data.brief.length) ? `<option value="brief">Today in brief</option>` : "";
    select.innerHTML = `<option value="">Topic…</option>${briefOption}${selectOptionsHtml(categories)}`;
    main.innerHTML = briefHtml(data.brief) + categories.map(sectionHtml).join("");
  } catch (err) {
    updated.textContent = "Not yet updated";
    banner.textContent = "Couldn't load today's digest yet. The first run happens on the next scheduled GitHub Actions job — check back soon, or trigger it manually from the Actions tab.";
    banner.classList.add("show");
    main.innerHTML = `<p class="empty-topic">No digest data found yet.</p>`;
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
