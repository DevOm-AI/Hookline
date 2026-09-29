"use strict";

// Everything on this page comes from the JSON API. Values (URLs, payloads, receivers' error
// bodies) are untrusted, so they only ever go in through textContent, never innerHTML.

const KEY_STORAGE = "hookline-api-key";
const REFRESH_MS = 10000;
const $ = (id) => document.getElementById(id);

let refreshTimer = null;
let openEventId = null;
let memoryKey = null;

function getKey() {
  try {
    return sessionStorage.getItem(KEY_STORAGE);
  } catch {
    return null;
  }
}

function setKey(key) {
  try {
    if (key) sessionStorage.setItem(KEY_STORAGE, key);
    else sessionStorage.removeItem(KEY_STORAGE);
  } catch {
    // Storage blocked: the key lasts until the page is reloaded.
  }
  memoryKey = key;
}

class Unauthorized extends Error {}

async function api(method, path) {
  const response = await fetch(path, {
    method,
    headers: { Authorization: `Bearer ${memoryKey}` },
  });
  if (response.status === 401) throw new Unauthorized();
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : response.statusText;
    throw new Error(`${method} ${path}: ${response.status} ${detail}`);
  }
  return body;
}

// --- DOM helpers ---

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (name === "class") node.className = value;
    else if (name === "onclick") node.addEventListener("click", value);
    else if (value !== undefined && value !== null) node[name] = value;
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function row(...cells) {
  return el("tr", {}, ...cells.map((cell) => (cell instanceof HTMLElement && cell.tagName === "TD" ? cell : el("td", {}, cell))));
}

function num(value) {
  return el("td", { class: "num" }, value);
}

function emptyRow(columns, text) {
  return el("tr", {}, el("td", { colSpan: columns, class: "muted empty" }, text));
}

function time(iso) {
  return iso ? new Date(iso).toLocaleString() : "";
}

function shortId(id) {
  return id.slice(0, 8);
}

function statusBadge(status) {
  return el("span", { class: `badge ${status}` }, status.replace("_", " "));
}

const STATUS_ORDER = ["succeeded", "dead", "in_progress", "pending"];

function countBadges(counts) {
  const entries = STATUS_ORDER.filter((status) => counts[status]).map((status) => [status, counts[status]]);
  if (entries.length === 0) return el("span", { class: "muted" }, "no subscribers");
  return el("span", {}, ...entries.map(([status, count]) => el("span", { class: `badge ${status}` }, `${count} ${status.replace("_", " ")}`)));
}

function eventLink(eventId) {
  return el("button", { type: "button", class: "link", title: eventId, onclick: () => openEvent(eventId) }, el("code", {}, shortId(eventId)));
}

function showMessage(text, kind = "info") {
  const message = $("message");
  message.textContent = text;
  message.className = kind;
  message.hidden = !text;
}

// --- sections ---

function renderEndpoints(endpoints, stats) {
  const statsById = new Map(stats.map((s) => [s.endpoint_id, s]));
  const rows = endpoints.map((endpoint) => {
    const s = statsById.get(endpoint.id) || { deliveries: {}, success_rate: null };
    const d = s.deliveries;
    const rate = s.success_rate === null ? "–" : `${(s.success_rate * 100).toFixed(1)}%`;
    const rateCell = num(rate);
    if (s.success_rate !== null && s.success_rate < 0.9) rateCell.classList.add("bad");
    return row(
      el("td", { class: "url", title: endpoint.url }, endpoint.url),
      endpoint.event_types.join(", "),
      endpoint.is_active ? statusBadge("active") : statusBadge("paused"),
      rateCell,
      num(d.succeeded || 0),
      num(d.dead || 0),
      num((d.pending || 0) + (d.in_progress || 0)),
      el("td", {}, el("button", { type: "button", class: "secondary", onclick: (e) => replayEndpoint(endpoint, e.currentTarget) }, "Replay dead")),
    );
  });
  $("endpoints").replaceChildren(...(rows.length ? rows : [emptyRow(8, "No endpoints registered.")]));
}

function renderDead(dead, endpointUrls) {
  $("dead-count").textContent = dead.length ? `(${dead.length})` : "";
  const rows = dead.map((delivery) => {
    const lastResult = delivery.last_status_code === null ? "" : `${delivery.last_status_code} · `;
    const error = `${lastResult}${delivery.last_error || "no attempt recorded"}`;
    return row(
      time(delivery.created_at),
      el("td", { class: "url", title: endpointUrls.get(delivery.endpoint_id) || delivery.endpoint_id }, endpointUrls.get(delivery.endpoint_id) || delivery.endpoint_id),
      el("td", {}, eventLink(delivery.event_id)),
      num(delivery.attempt_count),
      el("td", { class: "error", title: error }, error),
      el("td", {}, el("button", { type: "button", onclick: (e) => replayDelivery(delivery, e.currentTarget) }, "Replay")),
    );
  });
  $("dead").replaceChildren(...(rows.length ? rows : [emptyRow(6, "Nothing dead. Every delivery got through or is still being tried.")]));
}

function renderEvents(events) {
  const rows = events.map((event) =>
    row(time(event.created_at), el("td", {}, el("code", {}, event.type)), el("td", {}, eventLink(event.id)), el("td", {}, countBadges(event.deliveries))),
  );
  $("events").replaceChildren(...(rows.length ? rows : [emptyRow(4, "No events yet.")]));
}

function renderEventDetail(event) {
  $("detail-id").textContent = event.id;
  $("detail-type").replaceChildren(el("code", {}, event.type));
  $("detail-created").textContent = `created ${time(event.created_at)} · key ${event.idempotency_key}`;
  $("detail-payload").textContent = JSON.stringify(event.payload, null, 2);
  const blocks = event.deliveries.map((delivery) => {
    const attempts = delivery.attempts.map((attempt) =>
      row(
        time(attempt.created_at),
        num(attempt.status_code === null ? "–" : attempt.status_code),
        num(`${attempt.response_ms} ms`),
        el("td", { class: attempt.error ? "error wrap" : "wrap" }, attempt.error || "OK"),
      ),
    );
    return el(
      "div",
      { class: "delivery" },
      el("h3", {}, statusBadge(delivery.status), " ", delivery.endpoint_url),
      el("p", { class: "muted" }, `${delivery.attempt_count} attempt(s) since created or replayed · next attempt ${time(delivery.next_attempt_at)}`),
      el(
        "table",
        {},
        el("thead", {}, el("tr", {}, el("th", {}, "When"), el("th", { class: "num" }, "Status"), el("th", { class: "num" }, "Time"), el("th", {}, "Error"))),
        el("tbody", {}, ...(attempts.length ? attempts : [emptyRow(4, "Not tried yet.")])),
      ),
    );
  });
  $("detail-deliveries").replaceChildren(...(blocks.length ? blocks : [el("p", { class: "muted" }, "No endpoint was subscribed to this event type.")]));
  $("event-detail").hidden = false;
}

// --- actions ---

async function refresh() {
  try {
    const [endpoints, stats, dead, events] = await Promise.all([
      api("GET", "/endpoints"),
      api("GET", "/endpoints/stats"),
      api("GET", "/deliveries?status=dead&limit=100"),
      api("GET", "/events?limit=25"),
    ]);
    renderEndpoints(endpoints, stats);
    renderDead(dead, new Map(endpoints.map((e) => [e.id, e.url])));
    renderEvents(events);
    if (openEventId) renderEventDetail(await api("GET", `/events/${openEventId}`));
    $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    handleError(error);
  }
}

async function openEvent(eventId) {
  try {
    openEventId = eventId;
    renderEventDetail(await api("GET", `/events/${eventId}`));
    $("event-detail").scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    handleError(error);
  }
}

async function replayDelivery(delivery, button) {
  button.disabled = true;
  try {
    await api("POST", `/deliveries/${delivery.id}/replay`);
    showMessage(`Delivery ${shortId(delivery.id)} is pending again and will be sent shortly.`);
  } catch (error) {
    handleError(error);
  }
  await refresh();
}

async function replayEndpoint(endpoint, button) {
  button.disabled = true;
  try {
    const { replayed } = await api("POST", `/endpoints/${endpoint.id}/replay-dead`);
    showMessage(replayed ? `Replaying ${replayed} dead deliveries to ${endpoint.url}.` : `No dead deliveries for ${endpoint.url}.`);
  } catch (error) {
    handleError(error);
  }
  button.disabled = false;
  await refresh();
}

function handleError(error) {
  if (error instanceof Unauthorized) {
    showLogin("That API key was rejected.");
  } else {
    showMessage(error.message, "error");
  }
}

function showLogin(reason = "") {
  setKey(null);
  clearInterval(refreshTimer);
  refreshTimer = null;
  $("dashboard").hidden = true;
  $("toolbar").hidden = true;
  $("login").hidden = false;
  showMessage(reason, "error");
  $("api-key").focus();
}

function showDashboard() {
  $("login").hidden = true;
  $("dashboard").hidden = false;
  $("toolbar").hidden = false;
  showMessage("");
  refresh();
  refreshTimer = setInterval(() => {
    if (!document.hidden) refresh();
  }, REFRESH_MS);
}

document.addEventListener("DOMContentLoaded", () => {
  $("login").addEventListener("submit", (e) => {
    e.preventDefault();
    setKey($("api-key").value.trim());
    $("api-key").value = "";
    showDashboard();
  });
  $("refresh").addEventListener("click", () => {
    showMessage("");
    refresh();
  });
  $("forget").addEventListener("click", () => showLogin());
  $("detail-close").addEventListener("click", () => {
    openEventId = null;
    $("event-detail").hidden = true;
  });
  memoryKey = getKey();
  if (memoryKey) showDashboard();
  else showLogin();
});
