const CATEGORY_LABELS = {
  book: "书摊",
  umbrella: "修伞摊",
  tea: "茶摊",
  herb: "草药摊",
};

function catLabel(code) {
  return CATEGORY_LABELS[code] || code;
}

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body && typeof opts.body !== "string") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, opts);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) {
    const err = data?.error || { code: "HTTP_" + resp.status, message: text };
    throw new Error(`${err.code}: ${err.message}`);
  }
  return data;
}

function toast(message, isError = false) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.style.display = "block";
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => { el.style.display = "none"; }, 5200);
}

function jsonBlock(data) {
  return `<pre class="json">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[ch]));
}

function statusBadge(status) {
  const good = ["effective", "succeeded", "valid", "open", "qualified", "trading"].includes(status);
  const bad = ["reversed", "failed_owed", "rained_out", "withdrawn", "blocked", "ADJACENCY_REQUIRED"].includes(status);
  return `<span class="badge ${good ? "ok" : bad ? "bad" : "info"}">${escapeHtml(status)}</span>`;
}

function shell(active, title, subtitle) {
  const links = [
    ["/", "总览"],
    ["/street.html", "街坊看街讯"],
    ["/vendor.html", "摊主摆位"],
    ["/committee.html", "村委/联席会"],
  ];
  return `
  <header>
    <h1>甲乙丙 · 三乡轮市</h1>
    <p>${escapeHtml(title)} — ${escapeHtml(subtitle)}</p>
    <nav>${links.map(([href, label]) =>
      `<a href="${href}" class="${active === href ? "active" : ""}">${label}</a>`).join("")}
    </nav>
  </header>
  <main></main>`;
}
