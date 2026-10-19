"use strict";

// Helpers, the state every part shares, talking to the server, and the dialog over the lines.

const $ = (selector) => document.querySelector(selector);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;  // never innerHTML: names and port output are untrusted
  return node;
};

let secret = "";
let profiles = [];
let scripts = [];
const tabs = [];
let active = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-Termdeck-Auth": secret, "Content-Type": "application/json" },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

// The secret of this browser tab is kept for the life of the tab, so that a reload does not need a new link.
const storedSecret = {
  get() { try { return sessionStorage.getItem("termdeck.secret") || ""; } catch { return ""; } },
  set(value) { try { if (value) sessionStorage.setItem("termdeck.secret", value); else sessionStorage.removeItem("termdeck.secret"); } catch { /* no storage */ } },
};

async function login() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  history.replaceState(null, "", location.pathname);
  if (!token) {
    const kept = storedSecret.get();
    const response = kept ? await fetch("/api/ping", { headers: { "X-Termdeck-Auth": kept }, cache: "no-store" }).catch(() => null) : null;
    if (response?.ok) { secret = kept; return; }
    storedSecret.set("");
    throw new Error("Open termdeck with the link it printed.");
  }
  const response = await fetch("/api/login", { method: "POST", body: JSON.stringify({ token }) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error);
  secret = data.secret;
  storedSecret.set(secret);
}

function showText(node, text) {
  node.textContent = text || "";
  node.hidden = !text;
}

const summary = (p) => `${p.device} ${p.baud} ${p.dataBits}${p.parity[0].toUpperCase()}${p.stopBits}` +
  (p.flow && p.flow !== "none" ? ` ${p.flow === "rtscts" ? "RTS/CTS" : "XON/XOFF"}` : "");

// ---------------------------------------------------------------- views
// Editing a session or a script happens in a dialog over the lines.

const PAGES = ["#profile-form", "#script-form"];

function showPage(selector) {
  for (const page of PAGES) $(page).hidden = page !== selector;
  if (!$("#panel").open) $("#panel").showModal();
}

function showLines() {
  if ($("#panel").open) $("#panel").close();
  if (active) { fitAll(); requestAnimationFrame(() => active?.term.focus()); }
}
$("#panel").addEventListener("close", () => { if (active) active.term.focus(); });  // also after Esc

document.addEventListener("click", (e) => { if (e.target.closest("[data-back]")) showLines(); });
