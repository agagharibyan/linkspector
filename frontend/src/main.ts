import "./style.css";

type ScanStatus = "pending" | "running" | "completed" | "failed";
type IssueType = "broken_link" | "broken_image" | "missing_alt";
type ResourceType = "link" | "image" | "page";
type AltStatus = "ok" | "missing" | "empty";

interface ScanSummary {
  id: number;
  url: string;
  status: ScanStatus;
  max_depth: number;
  max_pages: number;
  pages_checked: number;
  links_checked: number;
  images_checked: number;
  broken_links: number;
  broken_images: number;
  missing_alt_tags: number;
  current_url: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

interface ScanItem {
  id: number;
  source_url: string;
  target_url: string;
  resource_type: ResourceType;
  issue_type: IssueType;
  status_code: number | null;
  alt_status: AltStatus | null;
  detail: string | null;
}

interface ScanDetail {
  scan: ScanSummary;
  items: ScanItem[];
  total_items: number;
}

const API_BASE = import.meta.env.VITE_API_BASE ?? "";
const POLL_INTERVAL_MS = 1200;

const ISSUE_LABELS: Record<IssueType, string> = {
  broken_link: "Broken Link",
  broken_image: "Broken Image",
  missing_alt: "Missing Alt Tag",
};

const ISSUE_BADGES: Record<IssueType, string> = {
  broken_link: "bg-rose-500/15 text-rose-300 ring-1 ring-inset ring-rose-500/25",
  broken_image: "bg-amber-500/15 text-amber-300 ring-1 ring-inset ring-amber-500/25",
  missing_alt: "bg-sky-500/15 text-sky-300 ring-1 ring-inset ring-sky-500/25",
};

const STATUS_BADGES: Record<ScanStatus, string> = {
  pending: "bg-slate-500/20 text-slate-300",
  running: "bg-sky-500/20 text-sky-300",
  completed: "bg-emerald-500/20 text-emerald-300",
  failed: "bg-rose-500/20 text-rose-300",
};

const el = <T extends HTMLElement>(id: string): T => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing element #${id}`);
  return node as T;
};

const dom = {
  form: el<HTMLFormElement>("scan-form"),
  urlInput: el<HTMLInputElement>("url-input"),
  depthInput: el<HTMLInputElement>("depth-input"),
  pagesInput: el<HTMLInputElement>("pages-input"),
  scanButton: el<HTMLButtonElement>("scan-button"),
  formError: el<HTMLParagraphElement>("form-error"),
  statusPanel: el<HTMLElement>("status-panel"),
  statusBadge: el<HTMLSpanElement>("status-badge"),
  statusTarget: el<HTMLSpanElement>("status-target"),
  statusDetail: el<HTMLParagraphElement>("status-detail"),
  progressBar: el<HTMLDivElement>("progress-bar"),
  exportButton: el<HTMLButtonElement>("export-button"),
  metrics: el<HTMLElement>("metrics"),
  results: el<HTMLElement>("results"),
  resultsBody: el<HTMLTableSectionElement>("results-body"),
  resultsEmpty: el<HTMLDivElement>("results-empty"),
  resultsFooter: el<HTMLDivElement>("results-footer"),
  filterPills: el<HTMLDivElement>("filter-pills"),
  statusFilter: el<HTMLSelectElement>("status-filter"),
  searchInput: el<HTMLInputElement>("search-input"),
  historySection: el<HTMLElement>("history-section"),
  historyList: el<HTMLUListElement>("history-list"),
};

const state = {
  scanId: null as number | null,
  detail: null as ScanDetail | null,
  issueFilter: "all" as IssueType | "all",
  statusFilter: "all" as string,
  search: "",
  pollTimer: null as number | null,
};

/** Mirrors the backend validation so obvious mistakes never hit the network. */
function validateUrl(raw: string): { ok: true; url: string } | { ok: false; message: string } {
  const trimmed = raw.trim();
  if (!trimmed) return { ok: false, message: "Enter a URL to scan." };

  const candidate = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(trimmed) ? trimmed : `https://${trimmed}`;

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return { ok: false, message: "That does not look like a valid URL." };
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { ok: false, message: "Only http:// and https:// URLs can be scanned." };
  }
  if (!parsed.hostname.includes(".") && parsed.hostname !== "localhost") {
    return { ok: false, message: "Enter a full hostname, for example example.com." };
  }
  return { ok: true, url: parsed.toString() };
}

function escapeHtml(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char] as string,
  );
}

function statusLabel(item: ScanItem): string {
  if (item.issue_type === "missing_alt") {
    return item.alt_status === "empty" ? "Empty alt" : "No alt";
  }
  if (item.status_code === null) {
    return item.detail?.toLowerCase().includes("timed out") ? "Timeout" : "Error";
  }
  return String(item.status_code);
}

function statusClass(item: ScanItem): string {
  if (item.issue_type === "missing_alt") return "text-sky-300";
  if (item.status_code === null) return "text-fuchsia-300";
  if (item.status_code >= 500) return "text-orange-300";
  return "text-rose-300";
}

function setFormError(message: string | null): void {
  if (message) {
    dom.formError.textContent = message;
    dom.formError.classList.remove("hidden");
  } else {
    dom.formError.classList.add("hidden");
  }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail) && body.detail.length) {
        const first = body.detail[0] as { msg?: string };
        if (first?.msg) message = first.msg;
      }
    } catch {
      /* response had no JSON body */
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

function renderMetrics(scan: ScanSummary): void {
  const cards = [
    { label: "Pages Checked", value: scan.pages_checked, accent: "text-white", hint: `${scan.links_checked} links · ${scan.images_checked} images` },
    { label: "Broken Links", value: scan.broken_links, accent: "text-rose-400", hint: "4xx / 5xx / unreachable" },
    { label: "Broken Images", value: scan.broken_images, accent: "text-amber-400", hint: "unloadable image sources" },
    { label: "Missing Alt Tags", value: scan.missing_alt_tags, accent: "text-sky-400", hint: "accessibility issues" },
  ];

  dom.metrics.innerHTML = cards
    .map(
      (card) => `
      <div class="rounded-2xl border border-white/10 bg-ink-900/70 p-5">
        <p class="text-xs font-medium uppercase tracking-wide text-slate-400">${card.label}</p>
        <p class="mt-2 text-3xl font-semibold ${card.accent}">${card.value}</p>
        <p class="mt-1 text-xs text-slate-500">${card.hint}</p>
      </div>`,
    )
    .join("");
  dom.metrics.classList.remove("hidden");
}

function renderFilters(items: ScanItem[]): void {
  const counts: Record<string, number> = { all: items.length };
  for (const item of items) counts[item.issue_type] = (counts[item.issue_type] ?? 0) + 1;

  const pills: Array<{ key: IssueType | "all"; label: string }> = [
    { key: "all", label: "All Issues" },
    { key: "broken_link", label: ISSUE_LABELS.broken_link },
    { key: "broken_image", label: ISSUE_LABELS.broken_image },
    { key: "missing_alt", label: ISSUE_LABELS.missing_alt },
  ];

  dom.filterPills.innerHTML = pills
    .map((pill) => {
      const active = state.issueFilter === pill.key;
      const classes = active
        ? "bg-white/10 text-white ring-1 ring-inset ring-white/20"
        : "text-slate-400 hover:text-slate-200";
      return `<button type="button" data-filter="${pill.key}" class="rounded-lg px-3 py-1.5 text-sm font-medium transition ${classes}">
        ${pill.label} <span class="ml-1 text-xs text-slate-500">${counts[pill.key] ?? 0}</span>
      </button>`;
    })
    .join("");

  const codes = [...new Set(items.map((item) => item.status_code).filter((c): c is number => c !== null))].sort(
    (a, b) => a - b,
  );
  const hasNoCode = items.some((item) => item.status_code === null && item.issue_type !== "missing_alt");
  const previous = state.statusFilter;

  dom.statusFilter.innerHTML = [
    `<option value="all">All status codes</option>`,
    ...codes.map((code) => `<option value="${code}">${code}</option>`),
    hasNoCode ? `<option value="none">Timeout / Error</option>` : "",
  ].join("");
  dom.statusFilter.value = [...dom.statusFilter.options].some((o) => o.value === previous)
    ? previous
    : "all";
  state.statusFilter = dom.statusFilter.value;
}

function visibleItems(items: ScanItem[]): ScanItem[] {
  const needle = state.search.trim().toLowerCase();
  return items.filter((item) => {
    if (state.issueFilter !== "all" && item.issue_type !== state.issueFilter) return false;
    if (state.statusFilter === "none") {
      if (item.status_code !== null || item.issue_type === "missing_alt") return false;
    } else if (state.statusFilter !== "all" && String(item.status_code) !== state.statusFilter) {
      return false;
    }
    if (needle) {
      const haystack = `${item.source_url} ${item.target_url}`.toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

function renderTable(items: ScanItem[], scan: ScanSummary): void {
  const rows = visibleItems(items);

  dom.resultsBody.innerHTML = rows
    .map((item) => {
      const detail = item.detail ? `<div class="mt-0.5 truncate text-xs text-slate-500">${escapeHtml(item.detail)}</div>` : "";
      return `<tr class="transition hover:bg-white/[0.03]">
        <td class="px-5 py-3 align-top">
          <a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer"
             class="cell-url block text-slate-300 hover:text-sky-300" title="${escapeHtml(item.source_url)}">
            ${escapeHtml(item.source_url)}
          </a>
        </td>
        <td class="px-5 py-3 align-top">
          <span class="cell-url block text-slate-200" title="${escapeHtml(item.target_url)}">${escapeHtml(item.target_url)}</span>
          ${detail}
        </td>
        <td class="px-5 py-3 align-top">
          <span class="whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-medium ${ISSUE_BADGES[item.issue_type]}">
            ${ISSUE_LABELS[item.issue_type]}
          </span>
        </td>
        <td class="px-5 py-3 align-top font-mono text-sm font-semibold ${statusClass(item)}">${statusLabel(item)}</td>
      </tr>`;
    })
    .join("");

  const running = scan.status === "pending" || scan.status === "running";
  if (rows.length === 0) {
    dom.resultsEmpty.textContent = items.length
      ? "No issues match the current filters."
      : running
        ? "Crawling… issues will appear here as they are found."
        : "No issues found. This site is clean.";
    dom.resultsEmpty.classList.remove("hidden");
  } else {
    dom.resultsEmpty.classList.add("hidden");
  }

  dom.resultsFooter.textContent = `Showing ${rows.length} of ${items.length} issue${items.length === 1 ? "" : "s"}`;
  dom.results.classList.remove("hidden");
}

function renderStatus(scan: ScanSummary): void {
  dom.statusPanel.classList.remove("hidden");
  dom.statusBadge.className = `rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wide ${STATUS_BADGES[scan.status]}`;
  dom.statusBadge.textContent = scan.status;
  dom.statusTarget.textContent = scan.url;

  const pct =
    scan.status === "completed" || scan.status === "failed"
      ? 100
      : Math.min(95, Math.round((scan.pages_checked / Math.max(1, scan.max_pages)) * 100) || 4);
  dom.progressBar.style.width = `${pct}%`;

  if (scan.status === "failed") {
    dom.statusDetail.textContent = scan.error ?? "Scan failed.";
    dom.statusDetail.className = "mt-2 truncate text-xs text-rose-400";
  } else if (scan.status === "completed") {
    dom.statusDetail.textContent = `Finished — ${scan.pages_checked} page(s) crawled, ${scan.links_checked} links and ${scan.images_checked} images inspected.`;
    dom.statusDetail.className = "mt-2 truncate text-xs text-slate-500";
  } else {
    dom.statusDetail.textContent = scan.current_url ? `Crawling ${scan.current_url}` : "Starting browser…";
    dom.statusDetail.className = "mt-2 truncate text-xs text-slate-400 pulse-dot";
  }

  dom.exportButton.disabled = scan.status === "pending";
}

function render(): void {
  if (!state.detail) return;
  const { scan, items } = state.detail;
  renderStatus(scan);
  renderMetrics(scan);
  renderFilters(items);
  renderTable(items, scan);
}

async function refreshHistory(): Promise<void> {
  try {
    const scans = await api<ScanSummary[]>("/api/scans?limit=8");
    if (!scans.length) return;
    dom.historyList.innerHTML = scans
      .map(
        (scan) => `<li>
          <button type="button" data-scan="${scan.id}"
            class="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left transition hover:bg-white/5">
            <span class="cell-url text-slate-300">${escapeHtml(scan.url)}</span>
            <span class="flex items-center gap-3 whitespace-nowrap text-xs">
              <span class="text-slate-500">${scan.broken_links + scan.broken_images + scan.missing_alt_tags} issues</span>
              <span class="rounded-full px-2 py-0.5 ${STATUS_BADGES[scan.status]}">${scan.status}</span>
            </span>
          </button>
        </li>`,
      )
      .join("");
    dom.historySection.classList.remove("hidden");
  } catch {
    /* history is a nicety; ignore failures */
  }
}

function stopPolling(): void {
  if (state.pollTimer !== null) {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

async function poll(): Promise<void> {
  if (state.scanId === null) return;
  const scanId = state.scanId;

  try {
    const detail = await api<ScanDetail>(`/api/scans/${scanId}`);
    if (state.scanId !== scanId) return;
    state.detail = detail;
    render();

    if (detail.scan.status === "pending" || detail.scan.status === "running") {
      state.pollTimer = window.setTimeout(poll, POLL_INTERVAL_MS);
      return;
    }
  } catch (error) {
    setFormError(error instanceof Error ? error.message : "Lost connection to the API.");
  }

  stopPolling();
  dom.scanButton.disabled = false;
  dom.scanButton.textContent = "Start Scan";
  void refreshHistory();
}

function watchScan(scanId: number): void {
  stopPolling();
  state.scanId = scanId;
  state.detail = null;
  state.issueFilter = "all";
  state.statusFilter = "all";
  state.search = "";
  dom.searchInput.value = "";
  void poll();
}

dom.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setFormError(null);

  const validation = validateUrl(dom.urlInput.value);
  if (!validation.ok) {
    setFormError(validation.message);
    dom.urlInput.focus();
    return;
  }

  dom.scanButton.disabled = true;
  dom.scanButton.textContent = "Scanning…";

  try {
    const scan = await api<ScanSummary>("/api/scans", {
      method: "POST",
      body: JSON.stringify({
        url: validation.url,
        max_depth: Number(dom.depthInput.value) || 0,
        max_pages: Number(dom.pagesInput.value) || 50,
      }),
    });
    watchScan(scan.id);
  } catch (error) {
    setFormError(error instanceof Error ? error.message : "Could not start the scan.");
    dom.scanButton.disabled = false;
    dom.scanButton.textContent = "Start Scan";
  }
});

dom.filterPills.addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-filter]");
  if (!button) return;
  state.issueFilter = button.dataset.filter as IssueType | "all";
  render();
});

dom.statusFilter.addEventListener("change", () => {
  state.statusFilter = dom.statusFilter.value;
  render();
});

dom.searchInput.addEventListener("input", () => {
  state.search = dom.searchInput.value;
  if (state.detail) renderTable(state.detail.items, state.detail.scan);
});

dom.exportButton.addEventListener("click", () => {
  if (state.scanId === null) return;
  window.location.href = `${API_BASE}/api/scans/${state.scanId}/export`;
});

dom.historyList.addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-scan]");
  if (!button) return;
  watchScan(Number(button.dataset.scan));
});

void refreshHistory();
