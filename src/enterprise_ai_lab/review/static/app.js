"use strict";

// Session data stays in memory. The server keeps bearer tokens behind its
// HttpOnly session cookie; this application never receives or stores them.
const state = {
  session: null,
  reviews: [],
  selected: null,
  detail: null,
  documentIndex: 0,
  activeView: "reviews",
  attempts: new Map(),
  decisionBusy: false,
  detailVersion: 0,
  messageTimer: null,
  expiryTimer: null,
};

const $ = (id) => document.getElementById(id);

function node(tag, options = {}, ...children) {
  const value = document.createElement(tag);
  if (options.className) value.className = options.className;
  if (options.text !== undefined) value.textContent = String(options.text);
  for (const [name, content] of Object.entries(options.attrs || {})) {
    if (content !== null && content !== undefined) value.setAttribute(name, String(content));
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) {
      value.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return value;
}

function testId(value) {
  return { "data-testid": value };
}

function showMessage(message) {
  clearTimeout(state.messageTimer);
  $("app-message").textContent = message;
  $("app-message").hidden = false;
  state.messageTimer = setTimeout(() => { $("app-message").hidden = true; }, 9000);
}

function sessionEnded(message = "Your session has ended. Sign in again to continue.") {
  clearTimeout(state.expiryTimer);
  state.session = null;
  state.reviews = [];
  state.detail = null;
  state.selected = null;
  state.attempts.clear();
  state.detailVersion += 1;
  $("workspace").hidden = true;
  $("header-session").hidden = true;
  $("session-identity").textContent = "";
  $("review-list").replaceChildren();
  $("review-detail").replaceChildren();
  $("audit-list").replaceChildren();
  $("landing").hidden = false;
  $("local-label").hidden = false;
  if (message) showMessage(message);
}

class HttpError extends Error {
  constructor(status) {
    super(`Request returned ${status}`);
    this.status = status;
  }
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") {
    if (!state.session?.csrf_token) throw new Error("No active session");
    headers["X-CSRF-Token"] = state.session.csrf_token;
  }
  try {
    const response = await fetch(path, {
      method: options.method || "GET",
      headers,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal: controller.signal,
      ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
    });
    if (!response.ok) {
      if (response.status === 401 && state.session) sessionEnded();
      throw new HttpError(response.status);
    }
    if (response.status === 204) return null;
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

function errorText(error, action) {
  if (error instanceof HttpError) {
    if (error.status === 401) return "Sign in again to continue.";
    if (error.status === 403) return "Your identity does not have permission for this action.";
    if (error.status === 404) return "This review is not available to your identity.";
    if (error.status === 409) return "The request has changed, expired or already received a different decision. Refresh its current state.";
    if (error.status === 422 || error.status === 400) return "The server rejected this request. Refresh before trying again.";
    if (error.status === 503) return "A local service is unavailable. Refresh when it is running again.";
  }
  return `${action} could not be confirmed. Check the local services and refresh.`;
}

function prettyTime(value) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return String(value);
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short" });
}

function statusLabel(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function statusPill(value) {
  const known = ["pending", "approved", "published", "rejected", "expired", "failed"];
  return node("span", { className: `status-pill${known.includes(value) ? ` status-${value}` : ""}`, text: statusLabel(value) });
}

function requestOf(value) {
  return value?.request || value || {};
}

function requestId(value) {
  return requestOf(value).request_id || value?.request_id || "";
}

function reviewPath(id) {
  return `/api/reviews/${encodeURIComponent(id)}`;
}

function emptyQueue(message) {
  return node("p", { className: "loading-note", text: message });
}

function renderQueue() {
  $("review-count").textContent = String(state.reviews.length);
  if (!state.reviews.length) {
    $("review-list").replaceChildren(emptyQueue("No reviews are available to this identity."));
    return;
  }
  $("review-list").replaceChildren(...state.reviews.map((review) => {
    const request = requestOf(review);
    const id = requestId(review);
    const documents = Array.isArray(review.documents) ? review.documents : [];
    const firstTitle = documents[0]?.title || "Document publication";
    const count = documents.length;
    const visibility = review.denied_document_count > 0 ? ` · ${review.denied_document_count} withheld` : "";
    const card = node("button", {
      className: "review-card",
      attrs: { type: "button", "aria-current": id === state.selected ? "true" : "false", ...testId(`review-card-${id}`) },
    },
    node("span", { className: "review-card-heading" },
      node("span", { className: "request-id", text: id }), statusPill(review.status)),
    node("span", { className: "review-card-title", text: firstTitle }),
    node("span", { className: "review-card-meta", text: `${count} visible document${count === 1 ? "" : "s"}${visibility} · tenant ${request.tenant_id || review.tenant_id || state.session.user.tenant_id}` }),
    node("span", { className: "review-card-meta review-card-publication", text: `Publication: ${statusLabel(review.publication_status)}` }));
    card.addEventListener("click", () => { void selectReview(id, true); });
    return card;
  }));
}

async function loadReviews() {
  const button = $("reviews-refresh");
  button.disabled = true;
  try {
    const result = await api("/api/reviews");
    if (!state.session) return;
    state.reviews = Array.isArray(result?.reviews) ? result.reviews : [];
    renderQueue();
    if (state.selected) await selectReview(state.selected);
    else if (state.reviews.length) await selectReview(requestId(state.reviews[0]));
  } catch (error) {
    if (state.session) showMessage(errorText(error, "Loading reviews"));
  } finally {
    button.disabled = false;
  }
}

function reconcileAttempt(detail) {
  const id = requestId(detail);
  const attempt = state.attempts.get(id);
  if (!attempt) return;
  const command = detail.receipt?.command;
  if (command?.command_id === attempt.body.command_id) {
    state.attempts.delete(id);
    showMessage("Your decision is recorded. Publication has its own status below.");
  } else if (command) {
    attempt.blocked = true;
    attempt.message = "This request already has a recorded decision. No new decision was sent.";
  } else if (requestOf(detail).snapshot_sha256 !== attempt.body.snapshot_sha256) {
    attempt.blocked = true;
    attempt.message = "The snapshot no longer matches this attempt. No new decision was sent.";
  }
}

async function selectReview(id, focus = false) {
  const version = ++state.detailVersion;
  const previous = state.selected;
  state.selected = id;
  if (previous !== id) state.documentIndex = 0;
  renderQueue();
  if (previous !== id || !state.detail) {
    $("review-detail").replaceChildren(node("p", { className: "loading-note", text: "Opening the immutable review snapshot…" }));
  }
  try {
    const detail = await api(reviewPath(id));
    if (!state.session || version !== state.detailVersion) return;
    state.detail = detail;
    const queuedIndex = state.reviews.findIndex((review) => requestId(review) === id);
    if (queuedIndex !== -1) {
      state.reviews[queuedIndex] = {
        ...state.reviews[queuedIndex],
        status: detail.status,
        publication_status: detail.publication_status,
        receipt: detail.receipt,
        effect: detail.effect,
      };
      renderQueue();
    }
    reconcileAttempt(detail);
    renderDetail();
    if (focus) $("review-heading")?.focus();
  } catch (error) {
    if (!state.session || version !== state.detailVersion) return;
    state.detail = null;
    $("review-detail").replaceChildren(node("p", { className: "error-note", text: errorText(error, "Opening this review") }));
  }
}

function metaItem(label, value) {
  return node("span", {}, `${label} `, node("strong", { text: value }));
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;
  const request = requestOf(detail);
  const documents = Array.isArray(detail.documents) ? detail.documents : [];
  if (state.documentIndex >= documents.length) state.documentIndex = 0;
  const refresh = node("button", { className: "text-button", text: "Refresh state ↻", attrs: { type: "button", ...testId("review-refresh") } });
  refresh.addEventListener("click", () => { void selectReview(state.selected); });
  const summary = node("section", { className: "review-summary", attrs: { "aria-labelledby": "review-heading" } },
    node("p", { className: "eyebrow", text: "FIXED CONTENT / FIXED ACCESS" }),
    node("div", { className: "review-summary-header" },
      node("h2", { text: request.request_id, attrs: { id: "review-heading", tabindex: "-1", ...testId("review-heading") } }),
      statusPill(detail.status)),
    node("div", { className: "summary-meta" },
      metaItem("Tenant", request.tenant_id),
      metaItem("Requested by", request.requested_by),
      metaItem("Base revision", request.expected_revision ?? "—"),
      metaItem("Publication", statusLabel(detail.publication_status))),
    node("div", { className: "snapshot-line" },
      node("span", { text: "SNAPSHOT SHA-256" }), node("code", { text: request.snapshot_sha256, attrs: testId("snapshot-digest") })));
  const container = node("div", {}, summary,
    node("div", { className: "section-title review-deadline" },
      node("p", { className: "queue-note", text: `Decision deadline: ${prettyTime(request.expires_at)}` }), refresh));
  if (detail.denied_document_count > 0) {
    container.append(node("p", { className: "permission-note", text: `${detail.denied_document_count} document${detail.denied_document_count === 1 ? " is" : "s are"} outside your document roles. Those PDFs and extracted words are withheld. You cannot decide a batch you cannot review in full.`, attrs: testId("denied-documents") }));
  }
  if (documents.length) {
    const select = node("select", { className: "document-select", attrs: { id: "document-select", ...testId("document-select") } });
    documents.forEach((document, index) => {
      select.append(node("option", { text: `${index + 1}. ${document.title}`, attrs: { value: index } }));
    });
    select.value = String(state.documentIndex);
    select.addEventListener("change", () => {
      state.documentIndex = Number(select.value);
      renderDocument(documents[state.documentIndex]);
    });
    container.append(node("div", { className: "documents-header" }, node("label", { text: "Source to inspect", attrs: { for: "document-select" } }), select));
    container.append(node("section", { attrs: { id: "document-inspector", "aria-label": "Selected document evidence" } }));
  } else {
    container.append(node("p", { className: "information-note", text: "No document text or PDF is available to your current document roles." }));
  }
  container.append(renderDecision(detail));
  $("review-detail").replaceChildren(container);
  if (documents.length) renderDocument(documents[state.documentIndex]);
}

function localPdfUrl(value) {
  if (typeof value !== "string" || !value.startsWith("/api/reviews/")) return null;
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/reviews/")) return null;
    url.hash = "page=1&view=FitH";
    return url.pathname + url.search + url.hash;
  } catch { return null; }
}

function localPreviewUrl(value) {
  if (typeof value !== "string" || !value.startsWith("/api/reviews/")) return null;
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/reviews/")
        || !url.pathname.endsWith("/preview.png") || url.hash) return null;
    return url.pathname + url.search;
  } catch { return null; }
}

function sourceLink(value) {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    return node("a", { text: value, attrs: { href: url.href, target: "_blank", rel: "noopener noreferrer", referrerpolicy: "no-referrer" } });
  } catch { return null; }
}

function sourceDetails(provenance, document) {
  const p = provenance || {};
  const entries = [
    ["Document ID", document.document_id],
    ["Original page", p.source_page],
    ["Artifact page", p.artifact_page],
    ["Extraction", p.method === "ocr" ? "OCR · word boxes and raw confidence retained in the source span" : p.method || "Not recorded"],
    ["Original URL", sourceLink(p.source_uri) || p.source_uri],
    ["Original SHA-256", p.source_sha256],
    ["Artifact SHA-256", p.artifact_sha256],
    ["Page text SHA-256", p.page_text_sha256],
    ["Full page span", p.start !== undefined && p.end !== undefined ? `[${p.start}, ${p.end}) Unicode code points` : undefined],
  ];
  const dl = node("dl");
  for (const [label, value] of entries) {
    if (value === undefined || value === null || value === "") continue;
    dl.append(node("dt", { text: label }), node("dd", {}, label.includes("SHA-256") ? node("code", { text: value }) : value));
  }
  return node("details", { className: "panel source-details", attrs: { ...testId("source-details") } }, node("summary", { text: "Origin & integrity" }), dl);
}

function renderDocument(document) {
  const provenance = document.provenance || {};
  const pdfUrl = localPdfUrl(document.pdf_url);
  const originalUrl = localPdfUrl(document.original_pdf_url);
  const previewUrl = localPreviewUrl(document.preview_url);
  const originalPreviewUrl = localPreviewUrl(document.original_preview_url);
  const hasDerivative = provenance.source_sha256 !== provenance.artifact_sha256
    || provenance.source_page !== provenance.artifact_page;
  const roles = Array.isArray(document.allowed_roles) ? document.allowed_roles : [];
  const access = node("div", { className: "document-access", attrs: testId("document-access") },
    node("span", { text: `Tenant ${document.tenant_id || requestOf(state.detail).tenant_id} · allowed roles` }),
    roles.map((role) => node("span", { className: "role-chip", text: role })),
    node("span", { text: provenance.method === "ocr" ? "· OCR page" : "· Native text page" }));
  const toolbar = node("div", { className: "pdf-toolbar" },
    node("span", { text: `Original p. ${provenance.source_page ?? "—"} · artifact p. ${provenance.artifact_page ?? "—"}` }));
  if (pdfUrl) toolbar.append(node("a", { text: "Open PDF ↗", attrs: { href: pdfUrl, target: "_blank", rel: "noopener", ...testId("pdf-open") } }));
  const viewer = node("section", { className: "panel", attrs: { "aria-label": "Authorized PDF page preview" } },
    node("div", { className: "panel-header" }, node("h3", { text: "Authorized page preview" }), node("span", { className: "panel-note", text: "PDFium · one permitted page" })), toolbar);
  if (previewUrl) {
    const image = node("img", { className: "page-preview", attrs: { id: "page-preview", alt: `${document.title} — authorized artifact page rendered from the PDF`, src: previewUrl, referrerpolicy: "no-referrer", ...testId("page-preview") } });
    image.addEventListener("error", () => {
      image.hidden = true;
      viewer.append(node("p", { className: "pdf-fallback error-note", text: "The authorized page image could not be loaded. Refresh the review or open its PDF before deciding." }));
    });
    viewer.append(image);
  } else {
    viewer.append(node("p", { className: "pdf-fallback", text: "A verified page image is not available for this document. Open the authorized PDF to inspect the source before deciding." }));
  }
  if (hasDerivative && originalPreviewUrl && originalPreviewUrl !== previewUrl) {
    viewer.append(node("p", { className: "pdf-fallback" }, "Compare with the ", node("a", { text: "authorized original page image ↗", attrs: { href: originalPreviewUrl, target: "_blank", rel: "noopener", ...testId("original-preview-open") } })));
  }
  if (hasDerivative && originalUrl && originalUrl !== pdfUrl) {
    viewer.append(node("p", { className: "pdf-fallback" }, "Comparing OCR? ", node("a", { text: "Open the authorized original page ↗", attrs: { href: originalUrl, target: "_blank", rel: "noopener", ...testId("original-pdf-open") } })));
  }
  const chunks = Array.isArray(document.chunks) ? document.chunks : [];
  const chunkList = node("div", { className: "chunk-list", attrs: { id: "chunk-list", tabindex: "0", "aria-label": "Exact extracted chunks", ...testId("chunk-list") } });
  chunks.forEach((chunk, index) => {
    const start = chunk.provenance?.start ?? chunk.start;
    const end = chunk.provenance?.end ?? chunk.end;
    const span = start !== undefined && end !== undefined ? `[${start}, ${end})` : "span not recorded";
    chunkList.append(node("article", { className: "chunk", attrs: testId(`chunk-${index}`) },
      node("div", { className: "chunk-meta" }, node("strong", { text: `CHUNK ${String(index + 1).padStart(2, "0")}` }), node("span", { text: span })),
      node("pre", { text: chunk.text || "" })));
  });
  if (!chunks.length) chunkList.append(node("p", { className: "loading-note", text: "No chunk spans were returned for this document." }));
  const snippets = node("section", { className: "panel", attrs: { "aria-label": "Extracted document snippets" } },
    node("div", { className: "panel-header" }, node("h3", { text: "What the RAG would receive" }), node("span", { className: "panel-note", text: `${chunks.length} chunk${chunks.length === 1 ? "" : "s"}` })), chunkList);
  const canonical = node("details", { className: "panel canonical-text" },
    node("summary", { text: "Read the complete extracted page" }),
    node("pre", { text: document.text || "", attrs: testId("page-text") }),
    node("p", { className: "text-note", text: "Chunk spans use half-open Unicode code point offsets in this exact canonical text. OCR confidence is an extraction diagnostic, not a verified transcription." }));
  $("document-inspector").replaceChildren(access, node("div", { className: "document-grid" }, viewer, snippets), sourceDetails(provenance, document), canonical);
}

function renderDecision(detail) {
  const request = requestOf(detail);
  const id = requestId(detail);
  const attempt = state.attempts.get(id);
  const box = node("section", { className: "decision-box", attrs: { "aria-labelledby": "decision-heading" } },
    node("h3", { text: "The decision belongs to this snapshot.", attrs: { id: "decision-heading" } }),
    node("p", { text: "Approval authorizes this exact content, source provenance and access policy. A recorded approval starts publication; it does not guarantee that publication has completed." }));
  const canDecide = detail.can_decide === true && detail.status === "pending" && !detail.receipt && !attempt?.blocked;
  const actions = node("div", { className: "decision-actions" });
  const approve = node("button", { className: "button button-approve", text: attempt?.body.decision === "approve" ? "Retry same approval ↗" : "Approve publication ✓", attrs: { id: "approve-button", type: "button", ...testId("approve-button") } });
  const reject = node("button", { className: "button button-reject", text: attempt?.body.decision === "reject" ? "Retry same rejection ↗" : "Reject publication ×", attrs: { id: "reject-button", type: "button", ...testId("reject-button") } });
  approve.disabled = !canDecide || state.decisionBusy || (attempt && attempt.body.decision !== "approve");
  reject.disabled = !canDecide || state.decisionBusy || (attempt && attempt.body.decision !== "reject");
  approve.addEventListener("click", () => { void decide("approve"); });
  reject.addEventListener("click", () => { void decide("reject"); });
  actions.append(approve, reject);
  box.append(actions);
  const message = node("div", { className: "decision-message", attrs: { id: "decision-status", role: "status", "aria-live": "polite", ...testId("decision-status") } });
  if (attempt) {
    message.classList.add("error");
    message.textContent = attempt.message || "Delivery is uncertain. Refresh the state first, then retry the same immutable decision if it remains pending. A different decision is disabled.";
  } else if (detail.receipt) {
    const command = detail.receipt.command || {};
    const publication = detail.publication_status;
    message.classList.add(command.decision === "approve" ? "success" : "error");
    message.textContent = command.decision === "approve"
      ? publication === "published" ? "Approval recorded. This snapshot is published and available to its permitted RAG readers." : `Approval recorded. Publication is ${statusLabel(publication)}. Refresh to check completion.`
      : "Rejection recorded. This request does not authorize publication.";
    box.append(node("div", { className: "decision-receipt" },
      `Decision by ${command.actor_id || "recorded identity"} · ${prettyTime(detail.receipt.accepted_at)}`, node("br"),
      "Command ", node("code", { text: command.command_id || "—" })));
  } else if (detail.status === "expired") {
    message.classList.add("error");
    message.textContent = "The decision deadline has passed. This request does not authorize publication.";
  } else if (!canDecide) {
    message.textContent = detail.denied_document_count > 0
      ? "A reviewer must be permitted to inspect every document in the batch before deciding."
      : request.requested_by === state.session.user.subject
        ? "The requester cannot approve or reject their own request. Use a different authorized reviewer."
        : "Your current identity cannot decide this request. Reading a review does not grant an approver role.";
  } else {
    message.textContent = "Compare the permitted PDF pages, extracted chunks and roles before making your decision.";
  }
  box.append(message);
  if (detail.effect) {
    box.append(node("p", { className: "decision-receipt", text: `Publication revision ${detail.effect.revision} · ${detail.effect.document_count} documents · ${detail.effect.chunk_count} chunks` }));
  }
  return box;
}

async function decide(decision) {
  if (state.decisionBusy || !state.detail || !state.session) return;
  const detail = state.detail;
  const id = requestId(detail);
  if (detail.can_decide !== true || detail.status !== "pending" || detail.receipt) return;
  let attempt = state.attempts.get(id);
  if (attempt && (attempt.blocked || attempt.body.decision !== decision)) return;
  if (!attempt) {
    attempt = {
      body: { command_id: `browser-${crypto.randomUUID()}`, snapshot_sha256: requestOf(detail).snapshot_sha256, decision },
      message: "Sending the decision. Its receipt and publication status will be checked separately.",
      blocked: false,
    };
    state.attempts.set(id, attempt);
  }
  state.decisionBusy = true;
  renderDetail();
  try {
    await api(`${reviewPath(id)}/decisions`, { method: "POST", body: attempt.body });
    attempt.message = "The decision endpoint responded. Refreshing the durable receipt…";
    await selectReview(id);
    // An HTTP acknowledgement alone is not treated as a durable receipt.
    if (state.attempts.has(id)) {
      attempt.message = "The durable receipt is not yet visible. Refresh before retrying the same decision.";
    }
    const list = await api("/api/reviews");
    if (state.session) { state.reviews = Array.isArray(list?.reviews) ? list.reviews : []; renderQueue(); }
  } catch (error) {
    if (!state.session) return;
    attempt.message = `${errorText(error, "Decision delivery")} The same command is retained for a safe retry; a different decision will not be sent.`;
    if (error instanceof HttpError && [400, 403, 404, 409, 422].includes(error.status)) attempt.blocked = true;
    await selectReview(id);
  } finally {
    state.decisionBusy = false;
    if (state.session && state.selected === id && state.detail) renderDetail();
  }
}

function selectTab(view) {
  if (view === "audit" && $("audit-tab").hidden) return;
  state.activeView = view;
  for (const name of ["reviews", "audit"]) {
    const selected = view === name;
    $(`${name}-tab`).setAttribute("aria-selected", String(selected));
    $(`${name}-tab`).tabIndex = selected ? 0 : -1;
    $(`${name}-panel`).hidden = !selected;
  }
  if (view === "audit") void loadAudit();
}

async function loadAudit() {
  $("audit-refresh").disabled = true;
  try {
    const result = await api("/api/audit");
    if (!state.session) return;
    const events = Array.isArray(result?.events) ? result.events : [];
    $("audit-integrity").replaceChildren(statusPill(result.chain_valid === true ? "verified" : "unverified"),
      document.createTextNode(result.chain_valid === true ? ` Local chain verified · ${events.length} visible events` : " Local chain could not be verified. Inspect the local service before relying on this log."));
    $("audit-integrity").setAttribute("data-testid", "audit-integrity");
    $("audit-list").replaceChildren(...(events.length ? events.map((event, index) => {
      const action = event.action || event.event_type || event.event || "Recorded event";
      const outcome = event.outcome || event.result || "recorded";
      const actor = event.subject || event.actor_id || event.actor || "local service";
      const id = event.request_id || event.details?.request_id || "—";
      return node("article", { className: "audit-event", attrs: testId(`audit-event-${index}`) },
        node("time", { className: "audit-time", text: prettyTime(event.recorded_at || event.timestamp || event.created_at || event.at) }),
        node("div", { className: "audit-description" }, node("strong", { text: statusLabel(action) }), node("p", { text: `Actor ${actor} · request ${id}` })),
        statusPill(typeof outcome === "string" ? outcome : "recorded"),
        node("details", {}, node("summary", { text: "Inspect event record" }), node("pre", { text: JSON.stringify(event, null, 2) })));
    }) : [emptyQueue("No audit events are available for this tenant.")]));
  } catch (error) {
    if (state.session) showMessage(errorText(error, "Loading the audit trail"));
  } finally {
    $("audit-refresh").disabled = false;
  }
}

async function start() {
  try {
    const session = await api("/api/session");
    if (!session?.authenticated || !session.user || !session.csrf_token) {
      sessionEnded("");
      return;
    }
    state.session = session;
    const remaining = Number(session.expires_at) * 1000 - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0) {
      sessionEnded();
      return;
    }
    state.expiryTimer = setTimeout(() => sessionEnded(), remaining);
    $("landing").hidden = true;
    $("local-label").hidden = true;
    $("workspace").hidden = false;
    $("header-session").hidden = false;
    $("session-identity").textContent = session.user.subject;
    $("scope-note").textContent = `Tenant ${session.user.tenant_id} · roles ${(session.user.roles || []).join(", ")}`;
    $("audit-tab").hidden = !(session.user.roles || []).includes("approver");
    await loadReviews();
  } catch (error) {
    sessionEnded(error instanceof HttpError && error.status === 401 ? "" : "The local review service could not be reached. Sign in when it is available.");
  }
}

$("reviews-refresh").addEventListener("click", () => { void loadReviews(); });
$("audit-refresh").addEventListener("click", () => { void loadAudit(); });
for (const view of ["reviews", "audit"]) {
  $(`${view}-tab`).addEventListener("click", () => selectTab(view));
  $(`${view}-tab`).addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const requested = event.key === "Home" ? "reviews" : event.key === "End" ? "audit" : view === "reviews" ? "audit" : "reviews";
    const next = requested === "audit" && $("audit-tab").hidden ? "reviews" : requested;
    selectTab(next);
    $(`${next}-tab`).focus();
  });
}
$("logout-button").addEventListener("click", async () => {
  $("logout-button").disabled = true;
  try {
    await api("/logout", { method: "POST", body: {} });
    sessionEnded("You have signed out of this local review session.");
  } catch (error) {
    if (state.session) showMessage(errorText(error, "Signing out"));
  } finally {
    $("logout-button").disabled = false;
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.session && Number(state.session.expires_at) * 1000 <= Date.now()) sessionEnded();
});

void start();
