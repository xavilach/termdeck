"use strict";

// ---------- helpers ----------

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node && typeof value !== "string") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) {
      node.append(child instanceof Node ? child : String(child));
    }
  }
  return node;
}

// ---------- authentication (see web/server.py) ----------

const AUTH_HEADER = "X-Termdeck-Auth";
let secret = null;

// The desktop window (gui.py) adds desktop=1 to the start link.
const desktop = (() => {
  try {
    if (new URLSearchParams(location.hash.slice(1)).get("desktop") === "1") sessionStorage.setItem("termdeck-desktop", "1");
    return sessionStorage.getItem("termdeck-desktop") === "1";
  } catch { return new URLSearchParams(location.hash.slice(1)).get("desktop") === "1"; }
})();

async function login() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) {
    history.replaceState(null, "", location.pathname);  // keep the token out of history
    const response = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "login failed");
    secret = data.secret;
    try { sessionStorage.setItem("termdeck-secret", secret); } catch { /* storage unavailable */ }
    return;
  }
  try { secret = sessionStorage.getItem("termdeck-secret"); } catch { secret = null; }
  if (!secret) throw new Error("Not logged in: open termdeck with the link it printed.");
}

async function api(method, path, body) {
  const headers = { [AUTH_HEADER]: secret };
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Object.assign(new Error(data.error || response.statusText), { field: data.field });
  return data;
}

// Shortcuts: Ctrl+Shift in the desktop window; in a browser Ctrl+Shift+N/W are the browser's own, so Alt+Shift.
const shortcut = (e) => (desktop ? e.ctrlKey && !e.altKey : e.altKey && !e.ctrlKey) && e.shiftKey && !e.metaKey;
const keyName = (key) => `${desktop ? "Ctrl" : "Alt"}+Shift+${key}`;

// Icons drawn inline (no font or emoji needed): 16x16 strokes in the text colour.
const ICONS = {
  plus: "M8 3v10M3 8h10",
  menu: "M2.5 4h11M2.5 8h11M2.5 12h11",
  up: "M8 13V3M4 7l4-4 4 4",
  refresh: "M13 8a5 5 0 1 1-1.5-3.5M13 2.5V5h-2.5",
  "folder-plus": "M1.5 4.5v8h13v-7H8L6.5 3.5h-5zM8 7.5v4M6 9.5h4",
  upload: "M8 11V3M4.5 6.5 8 3l3.5 3.5M2.5 13.5h11",
  folder: "M1.5 4v9h13V5.5H8L6.5 4z",
  file: "M3.5 1.5h6l3 3v10h-9zM9.5 1.5v3h3",
  link: "M7 9a2.5 2.5 0 0 0 3.5 0l2-2a2.5 2.5 0 0 0-3.5-3.5L8 4.5M9 7a2.5 2.5 0 0 0-3.5 0l-2 2A2.5 2.5 0 0 0 7 12.5l1-1",
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

let toastTimer;
function toast(message, isError = false) {
  const box = $("#toast");
  box.textContent = message;
  box.className = isError ? "error" : "";
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), isError ? 6000 : 3000);
}

function formatSize(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

const store = {
  get(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
  },
};

// ---------- terminal themes ----------

// Colour themes come from the server (themes.py): state.themes and each profile's palette.

// ---------- state ----------

const state = {
  options: null,     // option descriptions from the server
  defaults: null,    // default values per section
  profiles: [],
  tabs: [],
  active: null,
};
let tabIds = 0;

// ---------- profiles sidebar ----------

async function loadProfiles() {
  const { profiles, warnings, error, path } = await api("GET", "/api/profiles");
  state.profiles = profiles;
  renderProfiles();
  // Problems with the profile file stay visible in the sidebar instead of hiding profiles.
  const box = $("#profile-problems");
  box.hidden = !error && !warnings.length;
  if (error) {
    box.replaceChildren(el("strong", {}, "Profiles not loaded"), el("p", {}, error));
  } else if (warnings.length) {
    box.replaceChildren(el("details", {},
      el("summary", {}, `${warnings.length} profile${warnings.length > 1 ? "s" : ""} skipped`),
      el("p", { class: "muted" }, path),
      ...warnings.map((w) => el("p", {}, w))));
  }
}

// The list is one tab stop (a listbox): arrows move, Enter connects, F2 edits, Delete deletes,
// Shift+F10 or the menu key opens the actions.
function profileItem(p) {
  const item = el("div", {
    class: "profile", role: "option", tabindex: "-1", "aria-selected": "false",
    title: `${p.summary}\nDouble-click or Enter to connect`,
    ondblclick: () => connect(p),
    onclick: (e) => {
      focusProfile(item);
      if (e.detail === 1 && window.matchMedia("(pointer: coarse)").matches) connect(p);
    },
    oncontextmenu: (e) => { e.preventDefault(); focusProfile(item); profileMenu(p, e.clientX, e.clientY); },
  },
    el("span", { class: `kind ${p.kind}` }, p.kind === "ssh" ? "SSH" : "SER"),
    el("span", { class: "name" }, p.name),
    el("button", {
      class: "more", title: "Actions", tabindex: "-1",
      onclick: (e) => { e.stopPropagation(); const r = e.target.getBoundingClientRect(); profileMenu(p, r.left, r.bottom); },
    }, "⋯"),
    el("span", { class: "summary" }, p.summary),
  );
  item.profile = p;
  return item;
}

function focusProfile(item, focus = true) {
  for (const other of $("#profile-list").querySelectorAll(".profile")) {
    other.tabIndex = other === item ? 0 : -1;
    other.setAttribute("aria-selected", String(other === item));
  }
  if (focus) item?.focus();
}

function profileListKey(e) {
  const items = [...$("#profile-list").querySelectorAll(".profile")];
  const current = e.target.closest(".profile");
  if (!current) return;
  const i = items.indexOf(current);
  const move = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1, PageDown: i + 10, PageUp: i - 10 }[e.key];
  if (move !== undefined) {
    e.preventDefault();
    focusProfile(items[Math.max(0, Math.min(items.length - 1, move))]);
  } else if (e.key === "Enter") {
    e.preventDefault(); connect(current.profile);
  } else if (e.key === "F2") {
    e.preventDefault(); openEditor(current.profile);
  } else if (e.key === "Delete") {
    e.preventDefault(); deleteProfile(current.profile);
  } else if (e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey)) {
    e.preventDefault();
    const r = current.getBoundingClientRect();
    profileMenu(current.profile, r.left + 24, r.bottom);
  }
}

function renderProfiles() {
  const query = $("#filter").value.trim().toLowerCase();
  const visible = state.profiles.filter((p) =>
    !query || [p.name, p.group, p.summary].some((s) => (s || "").toLowerCase().includes(query)));
  const groups = new Map();
  for (const p of visible) {
    if (!groups.has(p.group)) groups.set(p.group, []);
    groups.get(p.group).push(p);
  }
  const names = [...groups.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
  const list = $("#profile-list");
  const focused = document.activeElement?.closest?.(".profile")?.profile?.name;
  list.replaceChildren(...names.flatMap((g) => [
    g ? el("div", { class: "group-title", role: "presentation" }, g)
      : (names.length > 1 ? el("div", { class: "group-title", role: "presentation" }, "Other") : null),
    ...groups.get(g).map(profileItem),
  ]));
  const items = [...list.querySelectorAll(".profile")];
  const keep = items.find((item) => item.profile.name === focused);
  focusProfile(keep || items[0], Boolean(keep));
  if (!state.profiles.length) {
    list.append(el("p", { class: "muted empty" }, "No profiles yet. Create one with + or use quick connect."));
  }
  renderRecent();
}

// Recently opened saved profiles, newest first, for the start page.
const RECENT = 8;
function rememberRecent(name) {
  store.set("recent", [name, ...store.get("recent", []).filter((n) => n !== name)].slice(0, RECENT));
}

function renderRecent() {
  const byName = new Map(state.profiles.map((p) => [p.name, p]));
  const recent = store.get("recent", []).map((n) => byName.get(n)).filter(Boolean);
  const shown = recent.length ? recent : state.profiles.slice(0, RECENT);
  $("#recent-title").textContent = recent.length ? "Recent sessions" : "Profiles";
  $("#recent").replaceChildren(...shown.map((p) =>
    el("li", {},
      el("button", { type: "button", class: "recent-item", onclick: () => connect(p), title: `Connect to ${p.name}` },
        el("span", { class: `kind ${p.kind}` }, p.kind === "ssh" ? "SSH" : "SER"),
        el("span", { class: "name" }, p.name),
        el("span", { class: "summary" }, p.summary)))));
  $("#recent-block").hidden = !shown.length;
}

function showMenu(x, y, items) {
  const menu = $("#menu");
  const opener = document.activeElement;
  const close = () => { menu.hidden = true; opener?.focus?.(); };
  menu.replaceChildren(...items.map(([label, action, cls]) =>
    el("button", { class: cls, role: "menuitem", onclick: () => { close(); action(); } }, label)));
  menu.onkeydown = (e) => {
    const buttons = [...menu.querySelectorAll("button")];
    const i = buttons.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      buttons[(i + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length].focus();
    }
  };
  menu.hidden = false;
  menu.style.left = `${Math.min(x, innerWidth - menu.offsetWidth - 8)}px`;
  menu.style.top = `${Math.min(y, innerHeight - menu.offsetHeight - 8)}px`;
  menu.querySelector("button")?.focus();
}

function profileMenu(p, x, y) {
  showMenu(x, y, [
    ["Connect", () => connect(p)],
    ["Edit…", () => openEditor(p)],
    ["Duplicate…", () => openEditor({ ...structuredClone(p), name: `${p.name} copy` }, { isNew: true })],
    ["Delete", () => deleteProfile(p), "danger"],
  ]);
}

async function deleteProfile(p) {
  const ok = await prompt({
    title: "Delete profile", body: el("p", {}, `Delete the profile ${p.name}? This cannot be undone.`),
    okLabel: "Delete", danger: true,
  });
  if (!ok) return;
  try {
    await api("DELETE", `/api/profiles/${encodeURIComponent(p.name)}`);
    await loadProfiles();
  } catch (e) { toast(e.message, true); }
}

// ---------- profile editor (built from the option descriptions) ----------

let editing = null;   // { original name or null, values }

function blankProfile(kind = "ssh") {
  return {
    name: "", kind, group: "",
    ssh: structuredClone(state.defaults.ssh),
    serial: structuredClone(state.defaults.serial),
    terminal: structuredClone(state.defaults.terminal),
  };
}

function withDefaults(p) {
  const base = blankProfile(p.kind);
  return {
    ...base, ...p,
    ssh: { ...base.ssh, ...p.ssh }, serial: { ...base.serial, ...p.serial }, terminal: { ...base.terminal, ...p.terminal },
  };
}

function fieldInput(opt, value, onChange) {
  const id = `f-${opt.key}-${Math.random().toString(36).slice(2, 7)}`;
  if (opt.choices) {
    const select = el("select", { id, onchange: () => onChange(JSON.parse(select.value)) },
      opt.choices.map(([k, label]) => el("option", { value: JSON.stringify(k), selected: k === value }, label)));
    return [id, select];
  }
  if (opt.type === "bool") {
    const box = el("input", { id, type: "checkbox", checked: Boolean(value), onchange: () => onChange(box.checked) });
    return [id, el("label", { class: "switch" }, box, el("span", { class: "slider" }))];
  }
  if (opt.widget === "multiline") {
    const area = el("textarea", { id, spellcheck: "false", oninput: () => onChange(area.value) });
    area.value = value ?? "";
    return [id, area];
  }
  const numeric = opt.type === "int" || opt.type === "float";
  const input = el("input", {
    id, spellcheck: "false",
    type: numeric ? "number" : (opt.widget === "secret" ? "password" : "text"),
    step: opt.type === "float" ? "any" : undefined,
    min: opt.min ?? undefined,
    max: opt.max ?? undefined,
    oninput: () => onChange(numeric ? Number(input.value) : input.value),
  });
  input.value = value ?? "";
  return [id, input];
}

function fieldRow(opt, values, sectionName) {
  const [id, input] = fieldInput(opt, values[opt.key], (v) => {
    values[opt.key] = v;
    row.classList.remove("invalid");
    input.removeAttribute("aria-invalid");
    if (opt.key === "kind") renderEditor();
    else applyWhen();
  });
  const help = opt.help ? el("small", { id: `${id}-help` }, opt.help) : null;
  if (help) input.setAttribute("aria-describedby", help.id);
  let recorded = null;
  if (opt.widget === "device") {
    attachPortSuggestions(input);
    // The USB adapter recorded when the profile was saved (Follow the device finds it again).
    if (values.usb_id) recorded = el("small", {}, `Recorded adapter: ${values.usb_id}`);
  }
  const row = el("label", {
    class: ["field", opt.type === "bool" && "bool", opt.wide && "wide", opt.advanced && "advanced"].filter(Boolean).join(" "),
    for: id, dataset: { field: `${sectionName}.${opt.key}` },
  },
    el("span", {}, opt.label, opt.advanced ? el("span", { class: "adv-badge" }, "adv") : null),
    input,
    help, recorded,
  );
  // Shown only while the option it depends on has one of the listed values (e.g. proxy fields).
  if (opt.when) {
    const { key, values: wanted } = opt.when;  // wanted null: shown while that option is set
    row.whenShown = () => (wanted === null ? Boolean(values[key]) : wanted.includes(values[key]));
  }
  return row;
}

function applyWhen() {
  for (const row of $("#editor-body").querySelectorAll(".field")) {
    if (row.whenShown) row.classList.toggle("unused", !row.whenShown());
  }
}

// Fields with subheadings for their group (advanced SSH options: Session, Proxy, Authentication...).
function grouped(options, values, sectionName) {
  let group = "";
  return options.flatMap((opt) => {
    const heading = opt.group && opt.group !== group
      ? el("h4", { class: `subsection ${opt.advanced ? "advanced" : ""}` }, opt.group) : null;
    group = opt.group || group;
    return [heading, fieldRow(opt, values, sectionName)];
  });
}

// Ports are listed again each time the field gets focus: an adapter plugged in meanwhile shows up.
async function attachPortSuggestions(input) {
  const list = el("datalist", { id: "serial-ports" });
  input.setAttribute("list", "serial-ports");
  input.after(list);
  const scan = async () => {
    try {
      const ports = await api("GET", "/api/serial-ports");
      list.replaceChildren(...ports.map((p) =>
        el("option", { value: p.device }, [p.description, p.usb_id].filter(Boolean).join(" · "))));
    } catch { /* suggestions are optional */ }
  };
  input.addEventListener("focus", scan);
  scan();
}

function section(title, rows, extraClass = "") {
  rows = rows.filter(Boolean);
  if (!rows.length) return null;
  const allAdvanced = rows.every((r) => r.classList.contains("advanced"));
  // The "adv" badge tells advanced fields apart in mixed sections; in an advanced section it only repeats the title.
  if (allAdvanced) rows.forEach((r) => r.querySelector(".adv-badge")?.remove());
  return el("div", { class: `section ${allAdvanced ? "advanced" : ""} ${extraClass}` },
    el("h3", {}, title), el("div", { class: "fields" }, rows));
}

// Empty means localhost: the server stores it that way.
const isLoopback = (host) => ["", "localhost", "127.0.0.1", "::1"].includes((host || "").trim());

function forwardsEditor(forwards) {
  const container = el("div", { class: "forwards" });
  const render = () => {
    container.replaceChildren(
      ...forwards.flatMap((f, i) => {
        const warning = el("small", { class: "warning", hidden: isLoopback(f.listen_host) },
          "Listens beyond this computer: other machines on the network can use this forward.");
        const input = (key, placeholder, type = "text") => {
          const node = el("input", {
            type, placeholder, spellcheck: "false", disabled: f.kind === "dynamic" && key.startsWith("dest"),
            oninput: () => {
              // An empty number stays empty (null): 0 is a real port ("any free port").
              f[key] = type !== "number" ? node.value : node.value === "" ? null : Number(node.value);
              warning.hidden = isLoopback(f.listen_host);
            },
          });
          node.value = f[key] ?? "";
          return node;
        };
        const kind = el("select", { onchange: () => { f.kind = kind.value; render(); } },
          [["local", "Local -L"], ["remote", "Remote -R"], ["dynamic", "SOCKS -D"]].map(([k, l]) =>
            el("option", { value: k, selected: f.kind === k }, l)));
        return [el("div", { class: "forward-row" },
          kind,
          input("listen_host", "localhost"), input("listen_port", "port", "number"),
          input("dest_host", f.kind === "dynamic" ? "—" : "destination host"),
          input("dest_port", "port", "number"),
          el("button", { type: "button", title: "Remove", onclick: () => { forwards.splice(i, 1); render(); } }, "✕")),
          warning];
      }),
      el("div", {},
        el("button", {
          type: "button",
          onclick: () => { forwards.push({ kind: "local", listen_host: "localhost", listen_port: null, dest_host: "", dest_port: null }); render(); },
        }, icon("plus"), "Add forward")),
    );
  };
  render();
  return container;
}

function renderEditor() {
  const v = editing.values;
  const kind = v.kind;
  const applies = (opt) => opt.kinds.includes(kind);
  const o = state.options;
  const conn = o[kind].filter((opt) => opt.type !== "forwards");
  const titles = state.sections;
  const body = $("#editor-body");
  body.replaceChildren(...[
    section(titles.profile, o.profile.map((opt) => fieldRow(opt, v, "profile"))),
    section(titles[kind], conn.filter((opt) => !opt.advanced).map((opt) => fieldRow(opt, v[kind], kind))),
    kind === "ssh"
      ? el("div", { class: "section", dataset: { field: "ssh.forwards" } }, el("h3", {}, titles.forwards), forwardsEditor(v.ssh.forwards))
      : null,
    section(`${titles[kind]} (advanced)`, grouped(conn.filter((opt) => opt.advanced), v[kind], kind)),
    section(titles.terminal, grouped(o.terminal.filter(applies), v.terminal, "terminal")),
  ].filter(Boolean));
  applyWhen();
}

function openEditor(profile = null, { isNew = !profile } = {}) {
  editing = { original: isNew ? null : profile.name, values: profile ? withDefaults(structuredClone(profile)) : blankProfile() };
  editing.snapshot = JSON.stringify(editing.values);
  $("#editor-title").textContent = isNew ? "New profile" : `Edit ${profile.name}`;
  $("#editor-error").hidden = true;
  renderEditor();
  $("#editor").showModal();
  $("#editor-body").querySelector("input")?.focus();
}

// Cancel and Esc ask before throwing away what was typed.
async function cancelEditor() {
  if (JSON.stringify(editing.values) !== editing.snapshot) {
    const discard = await prompt({
      title: "Discard changes?", body: el("p", {}, "The changes to this profile are not saved."),
      okLabel: "Discard", danger: true,
    });
    if (!discard) return;
  }
  $("#editor").close("cancel");
}

function showEditorError(e) {
  const box = $("#editor-error");
  box.textContent = e.message;
  box.hidden = false;
  const row = e.field && $("#editor-body").querySelector(`[data-field="${CSS.escape(e.field)}"]`);
  if (!row) return;
  if (row.closest(".advanced") && !$("#advanced-toggle").checked) $("#advanced-toggle").click();
  row.classList.add("invalid");
  const input = row.querySelector("input, select, textarea");
  input?.setAttribute("aria-invalid", "true");
  input?.setAttribute("aria-errormessage", "editor-error");
  (input || row).focus?.();
  row.scrollIntoView({ block: "nearest" });
}

function profilePayload(v) {
  // Only the active connection section is sent; the server ignores the other one anyway.
  const payload = { name: v.name.trim(), kind: v.kind, group: v.group.trim(), terminal: v.terminal };
  payload[v.kind] = v[v.kind];
  if (v.kind === "ssh") {
    // Rows nobody filled in are dropped; half-filled ones are reported, never lost silently.
    const forwards = v.ssh.forwards.filter((f) => f.listen_port != null || f.dest_host || f.dest_port != null);
    forwards.forEach((f, i) => {
      if (f.listen_port == null) throw new Error(`Port forward ${i + 1}: enter the listen port`);
      if (f.kind === "dynamic") Object.assign(f, { dest_host: "", dest_port: 0 });
      else if (f.dest_port == null) throw new Error(`Port forward ${i + 1}: enter the destination port`);
    });
    payload.ssh = { ...v.ssh, forwards };
  }
  return payload;
}

async function saveEditor(connectAfter) {
  const buttons = [$("#editor-save"), $("#editor-save-connect")];
  if (buttons[0].disabled) return;  // a save is already running (Enter pressed twice)
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const payload = profilePayload(editing.values);
    const saved = editing.original === null
      ? await api("POST", "/api/profiles", payload)
      : await api("PUT", `/api/profiles/${encodeURIComponent(editing.original)}`, payload);
    $("#editor").close("saved");
    // Open tabs of this profile take the saved version (and its new name, if renamed): their reconnect uses it.
    for (const tab of state.tabs) {
      if (tab.saved && tab.profile.name === editing.original) tab.profile = withDefaults(saved);
    }
    if (editing.original !== null && editing.original !== saved.name) {
      store.set("recent", store.get("recent", []).map((n) => (n === editing.original ? saved.name : n)));
    }
    renderTabs();
    renderToolbar();
    await loadProfiles();
    if (connectAfter) connect(saved);
  } catch (e) {
    showEditorError(e);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

// ---------- prompts (password, host key) ----------

let promptQueue = Promise.resolve();
let armTimer;

// `danger` styles OK as destructive and focuses Cancel. `armDelay` also keeps OK
// disabled for a moment, for prompts that can pop up while the user is typing.
function prompt({ title, body, input, okLabel = "OK", danger = false, armDelay = false }) {
  const run = () => new Promise((resolve) => {
    const dialog = $("#prompt");
    $("#prompt-title").textContent = title;
    let field = null;
    if (input) {
      field = el("input", { type: input.secret ? "password" : "text", autocomplete: "off", spellcheck: "false" });
    }
    $("#prompt-body").replaceChildren(...[body, field].flat().filter(Boolean));
    const ok = $("#prompt-ok");
    ok.textContent = okLabel;
    ok.className = danger ? "danger" : "primary";
    ok.disabled = armDelay;
    clearTimeout(armTimer);
    if (armDelay) armTimer = setTimeout(() => { ok.disabled = false; }, 1500);
    dialog.onclose = () => {
      const accepted = dialog.returnValue === "ok";
      resolve(accepted ? (field ? field.value : true) : (field ? null : false));
      if (field) field.value = "";  // do not keep secrets in the page
    };
    dialog.returnValue = "";
    dialog.showModal();
    (field || (danger || armDelay ? $("#prompt-cancel") : ok)).focus();
  });
  const result = promptQueue.then(run);
  promptQueue = result.catch(() => {});
  return result;
}

// ---------- tabs and terminals ----------

function connect(profile, { saved = true } = {}) {
  if (saved) rememberRecent(profile.name);
  document.body.classList.remove("sidebar-open");  // a narrow window shows the terminal
  const tab = {
    id: ++tabIds,
    profile: withDefaults(profile),
    saved,
    status: "connecting",
    ws: null,
    session: null,
    caps: { send_break: false, sftp: false, typing: true },  // what the server says the session can do
    forwards: [],
    forwardsSummary: "",
    sftpPath: null,
    sftpOpen: false,
    fontSize: profile.terminal?.font_size || 14,
  };
  tab.term = new Terminal({ ...terminalOptions(tab.profile, tab.fontSize), allowProposedApi: true, rightClickSelectsWord: false });
  const colours = tab.term.options.theme;
  tab.fit = new FitAddon.FitAddon();
  tab.search = new SearchAddon.SearchAddon();
  tab.term.loadAddon(tab.fit);
  tab.term.loadAddon(tab.search);
  tab.term.loadAddon(new WebLinksAddon.WebLinksAddon());
  tab.term.loadAddon(new Unicode11Addon.Unicode11Addon());
  tab.term.unicode.activeVersion = "11";

  tab.container = el("div", { class: "term", style: `background:${colours.background}` });
  $("#terminals").append(tab.container);
  tab.term.open(tab.container);
  setupClipboard(tab);
  tab.term.onBell(() => ringBell(tab));
  tab.term.onTitleChange((title) => { tab.remoteTitle = cleanTitle(title); renderTabs(); });
  // Every paste (Shift+Insert, the browser's Paste, middle click...) is cleaned, not only ours.
  tab.container.addEventListener("paste", (e) => {
    e.preventDefault();
    e.stopPropagation();
    pasteText(tab, e.clipboardData?.getData("text/plain") || "");
  }, true);
  tab.term.onData((data) => send(tab, { type: "input", data }));
  tab.term.onResize(({ cols, rows }) => { send(tab, { type: "resize", cols, rows }); updateStatusbar(); });
  tab.resizer = new ResizeObserver(() => { if (!tab.container.hidden) tab.fit.fit(); });
  tab.resizer.observe(tab.container);

  state.tabs.push(tab);
  activate(tab);
  openSocket(tab);
  return tab;
}

// xterm.js options from the profile's terminal settings.
function terminalOptions(profile, fontSize) {
  const t = profile.terminal;
  const mono = getComputedStyle(document.body).getPropertyValue("--mono");
  return {
    // Each family quoted on its own: "Cascadia Mono, Consolas" is two fonts, tried in order.
    fontFamily: t.font_family
      ? [...t.font_family.split(",").map((f) => f.trim().replace(/["\\]/g, "")).filter(Boolean).map((f) => `"${f}"`), mono].join(", ")
      : mono,
    fontSize,
    scrollback: t.scrollback,
    theme: profile.palette || state.themes[t.theme] || state.themes.dark,
    cursorStyle: t.cursor_style,
    cursorBlink: t.cursor_blink,
    fontWeightBold: t.bold_style === "colour" ? "normal" : "bold",
    drawBoldTextInBrightColors: t.bold_style !== "font",
    scrollOnUserInput: t.scroll_on_key,
    // PuTTY lists the characters that belong to words; xterm.js the ones that separate them.
    wordSeparator: [...WORD_SEPARATORS].filter((c) => !(t.word_chars || "").includes(c)).join(""),
  };
}

// A reconnect may bring options saved elsewhere since the tab opened (the font size stays the tab's).
function applyTerminalOptions(tab) {
  Object.assign(tab.term.options, terminalOptions(tab.profile, tab.fontSize));
  tab.container.style.background = tab.term.options.theme.background;
}

// Double click selects words: all ASCII punctuation separates them, except _-./~ and the profile's word characters.
const WORD_SEPARATORS = " !\"#$%&'()*+,:;<=>?@[\\]^`{|}";
let audio = null;

// Same rules as vt.BellLimiter: one bell per 250 ms, silence for 5 s after more than 5 in 2 s.
function bellAllowed(tab) {
  const now = performance.now();
  tab.bells = (tab.bells || []).filter((t) => now - t < 2000).concat(now);
  if (tab.bells.length > 5) tab.bellQuiet = now + 5000;
  if (now < (tab.bellQuiet || 0) || now - (tab.lastBell || -1e9) < 250) return false;
  tab.lastBell = now;
  return true;
}

// Same rules as vt.clean_title: one line, no control or direction characters, short.
function cleanTitle(title) {
  title = title.replace(/[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g, " ").split(/\s+/).filter(Boolean).join(" ");
  return title.length <= 60 ? title : title.slice(0, 59) + "…";
}

function ringBell(tab) {
  const style = tab.profile.terminal.bell;
  if (style === "none" || !bellAllowed(tab)) return;
  if (style === "visual" || style === "both") {
    tab.container.classList.add("flash");
    setTimeout(() => tab.container.classList.remove("flash"), 150);
  }
  if (style === "sound" || style === "both") {
    try {  // a short beep; no sound file needed
      audio ??= new AudioContext();
      const tone = audio.createOscillator(), gain = audio.createGain();
      tone.frequency.value = 880; gain.gain.value = 0.08;
      tone.connect(gain).connect(audio.destination);
      tone.start(); tone.stop(audio.currentTime + 0.12);
    } catch { /* no audio */ }
  }
  if (tab !== state.active && !tab.bell) { tab.bell = true; renderTabs(); }  // shown until the tab is opened
}

function pasteText(tab, text) {
  // Same rule as vt.clean_paste: no control characters (an ESC could end bracketed paste), unless allowed.
  if (!tab.profile.terminal.paste_controls) text = text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/g, "");
  if (text) tab.term.paste(text);
}

function setupClipboard(tab) {
  const term = tab.term;
  // PuTTY habits: selecting copies, right click pastes. Ctrl+Shift+C/V also work.
  term.onSelectionChange(() => {
    const text = term.getSelection();
    if (text) navigator.clipboard?.writeText(text).catch(() => {});
  });
  const paste = () => navigator.clipboard?.readText().then((text) => pasteText(tab, text))
    .catch(() => toast("Clipboard not available", true));
  tab.container.addEventListener("contextmenu", (e) => { e.preventDefault(); paste(); });
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown" || !e.ctrlKey || !e.shiftKey) return true;
    const key = e.key.toLowerCase();
    // preventDefault: the browser's own Ctrl+Shift+C (inspector) and Ctrl+Shift+V (a second, native paste).
    if (key === "c") { e.preventDefault(); navigator.clipboard?.writeText(term.getSelection()); return false; }
    if (key === "v") { e.preventDefault(); paste(); return false; }
    if (key === "f") { e.preventDefault(); openSearch(); return false; }
    return true;
  });
}

function send(tab, message) {
  if (tab.ws?.readyState === WebSocket.OPEN) tab.ws.send(JSON.stringify(message));
}

function openSocket(tab) {
  tab.remoteTitle = "";  // from the previous connection
  tab.bell = false;
  setStatus(tab, "connecting");
  tab.overlay?.remove();
  tab.reconnectKey?.dispose();
  tab.reconnectKey = null;
  tab.fit.fit();
  const ws = new WebSocket(`ws://${location.host}/api/terminal`, ["termdeck", `auth.${secret}`]);
  ws.binaryType = "arraybuffer";
  tab.ws = ws;
  ws.onopen = () => {
    const open = { type: "open", cols: tab.term.cols, rows: tab.term.rows };
    if (tab.saved) open.name = tab.profile.name;
    else open.profile = profilePayload(tab.profile);
    ws.send(JSON.stringify(open));
  };
  ws.onmessage = async (event) => {
    if (typeof event.data !== "string") {
      tab.term.write(new Uint8Array(event.data));
      if (tab.profile.terminal.scroll_on_output) tab.term.scrollToBottom();
      return;
    }
    const m = JSON.parse(event.data);
    if (m.type === "prompt") {
      activate(tab);
      const note = tab.note ? el("p", { class: "warning note" }, tab.note) : null;
      tab.note = null;
      const value = await prompt({ title: tab.profile.name, body: [note, el("p", {}, m.text)], input: { secret: m.secret } });
      send(tab, { type: "answer", id: m.id, value });
    } else if (m.type === "notice") {
      // Server banner, login instructions: dimmed, so it reads as a message, not output.
      tab.term.write(`\r\n\x1b[90m${m.text.replace(/\n/g, "\r\n")}\x1b[0m\r\n`);
      tab.note = m.text;  // also shown in the next question, which would hide the terminal
    } else if (m.type === "hostkey") {
      activate(tab);
      const body = [
        m.changed
          ? el("p", { class: "warning" }, `WARNING: the host key of ${m.host}:${m.port} has changed. This can mean someone is intercepting the connection, or that the server was reinstalled.`)
          : el("p", {}, `The server ${m.host}:${m.port} is not known yet. Check its key fingerprint before trusting it.`),
        el("div", { class: "fingerprint" }, `${m.key_type} ${m.fingerprint}`),
        ...m.old_fingerprints.map((f) => el("div", { class: "fingerprint muted" }, `Was: ${f}`)),
        el("p", { class: "muted" }, `Trusted keys are saved in ${m.known_hosts}`),
      ];
      const trusted = await prompt({
        title: m.changed ? "Host key changed" : "Unknown host", body,
        okLabel: m.changed ? "Trust new key" : "Trust and connect", danger: m.changed,
        armDelay: true,  // a host key prompt can open while the user types in a terminal
      });
      send(tab, { type: "answer", id: m.id, value: trusted });
    } else if (m.type === "break") {
      toast(m.message, !m.ok);
    } else if (m.type === "state" && m.state === "connecting") {
      setStatus(tab, "connecting");
    } else if (m.type === "state" && m.state === "connected") {
      Object.assign(tab, {
        session: m.session, caps: m.capabilities, forwards: m.forwards, forwardsSummary: m.forwards_summary,
        forwardsFailed: m.forwards_failed, sftpPath: null,
      });
      // The profile as the server connected it (saved profiles are read again on each connect).
      tab.profile = withDefaults(m.profile);
      applyTerminalOptions(tab);
      tab.term.options.disableStdin = !tab.caps.typing;  // a forwards-only connection: nothing to type into
      setStatus(tab, "connected");
      tab.term.focus();
      if (tab.sftpOpen) refreshSftp();
    } else if (m.type === "state") {  // failed (never connected) or ended
      tab.ended = { state: m.state, message: m.message, closeTab: Boolean(m.close_tab) };
      if (m.state === "failed") tab.term.write(`\r\n\x1b[31m${m.message}\x1b[0m\r\n`);
    }
  };
  ws.onclose = () => {
    if (tab.ws !== ws) return;
    tab.session = null;
    const ended = tab.ended || { state: tab.status === "connecting" ? "failed" : "ended", message: "Disconnected" };
    tab.ended = null;
    if (ended.closeTab) {  // Close tab on exit (the server applies the profile's rule)
      toast(`${tab.profile.name}: session ended, tab closed`);
      closeTab(tab, { ask: false });
      return;
    }
    setStatus(tab, ended.state);
    showReconnect(tab, ended.message);
  };
}

function showReconnect(tab, message) {
  tab.overlay?.remove();
  tab.overlay = el("div", { class: "overlay" },
    el("span", {}, message),
    el("button", { class: "primary", onclick: () => openSocket(tab) }, "Reconnect"),
    el("button", { onclick: () => closeTab(tab, { ask: false }) }, "Close tab"));
  tab.container.append(tab.overlay);
  tab.reconnectKey = tab.term.onKey(({ domEvent }) => {
    if (!isLive(tab) && domEvent.key === "Enter") openSocket(tab);
  });
}

const isLive = (tab) => tab.status === "connecting" || tab.status === "connected";
const STATUS_TEXT = { connecting: "Connecting…", connected: "Connected", failed: "Failed", ended: "Ended" };

function setStatus(tab, status) {
  tab.status = status;
  renderTabs();
  if (tab === state.active) renderToolbar();
}

function activate(tab) {
  state.active = tab;
  if (tab) tab.bell = false;
  for (const t of state.tabs) t.container.hidden = t !== tab;
  $("#welcome").hidden = Boolean(tab);
  $("#workspace").hidden = !tab;
  closeSearch();
  renderTabs();
  renderToolbar();
  if (tab) {
    requestAnimationFrame(() => { tab.fit.fit(); tab.term.focus(); });
    $("#sftp").hidden = !(tab.sftpOpen && tab.caps.sftp);
    if (tab.sftpOpen) refreshSftp();
  }
}

// Like PuTTY's Warn on close: closing a connected session asks first (unless the profile says not to).
async function closeTab(tab, { ask = true } = {}) {
  if (ask && tab.status === "connected" && tab.profile.terminal.warn_on_close) {
    const ok = await prompt({
      title: "Close session?", body: el("p", {}, `${tab.profile.name} is still connected. Close it?`),
      okLabel: "Close", danger: true,
    });
    if (!ok || !state.tabs.includes(tab)) return;
  }
  const ws = tab.ws;
  tab.ws = null;
  ws?.close();
  tab.resizer.disconnect();
  tab.term.dispose();
  tab.container.remove();
  const index = state.tabs.indexOf(tab);
  state.tabs.splice(index, 1);
  if (state.active === tab) activate(state.tabs[Math.min(index, state.tabs.length - 1)] || null);
  else renderTabs();
}

function renderTabs() {
  $("#tabs").replaceChildren(...state.tabs.map((tab) =>
    el("div", {
      class: `tab ${tab === state.active ? "active" : ""}`, role: "tab", "aria-selected": String(tab === state.active),
      title: `${tab.remoteTitle ? `${tab.profile.name}: ${tab.remoteTitle}` : tab.profile.name} (${STATUS_TEXT[tab.status]})`,
      onclick: () => activate(tab),
      onauxclick: (e) => { if (e.button === 1) closeTab(tab); },
    },
      el("span", { class: `dot ${tab.status}`, "aria-label": STATUS_TEXT[tab.status] }),
      el("span", { class: "label" }, tab.remoteTitle ? `${tab.profile.name}: ${tab.remoteTitle}` : tab.profile.name),
      tab.bell ? el("span", { class: "bell", title: "Bell rang" }, "!") : null,
      el("button", { class: "close", title: `Close (${keyName("W")})`, "aria-label": "Close tab",
        onclick: (e) => { e.stopPropagation(); closeTab(tab); } }, "✕"))));
}

function renderToolbar() {
  const tab = state.active;
  $("#toolbar").hidden = !tab;
  if (!tab) { updateStatusbar(); return; }
  $("#tb-status").textContent = STATUS_TEXT[tab.status];
  $("#tb-status").className = `status ${tab.status}`;
  $("#tb-summary").textContent = tab.profile.summary || "";
  const show = (action, visible) => { $(`[data-action=${action}]`).hidden = !visible; };
  // Only the controls the session has (the server reports them once connected).
  const live = tab.status === "connected";
  show("sftp", tab.caps.sftp);
  show("break", tab.caps.send_break);
  show("reconnect", !isLive(tab));
  $("[data-action=sftp]").disabled = !live;
  $("[data-action=sftp]").classList.toggle("on", tab.sftpOpen);
  $("[data-action=break]").disabled = !live;
  updateStatusbar();
}

function updateStatusbar() {
  const tab = state.active;
  $("#sb-left").textContent = tab ? `${tab.profile.name}: ${STATUS_TEXT[tab.status]}` : `${state.profiles.length} profiles`;
  const forwards = $("#sb-forwards");
  forwards.hidden = !(tab && tab.forwardsSummary && tab.status === "connected");
  if (!forwards.hidden) {
    forwards.textContent = tab.forwardsSummary;
    forwards.title = tab.forwards.map((f) => f.text).join("\n");
    forwards.classList.toggle("bad", tab.forwardsFailed > 0);
  }
  $("#sb-right").textContent = tab ? `${tab.term.cols}×${tab.term.rows}  ${tab.profile.terminal.encoding}` : "";
}

const toolbarActions = {
  reconnect: () => openSocket(state.active),
  break: () => send(state.active, { type: "break" }),  // the server answers whether it was sent
  "font-up": () => setFont(1),
  "font-down": () => setFont(-1),
  search: () => openSearch(),
  // Desktop window only (gui.py DesktopApi), like PuTTY's Always on top and full screen.
  fullscreen: () => window.pywebview?.api.toggle_fullscreen(),
  ontop: async () => {
    const button = $("[data-action=ontop]");
    const on = Boolean(await window.pywebview?.api.set_on_top(button.getAttribute("aria-pressed") !== "true"));
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", String(on));
    button.textContent = on ? "On top ✓" : "On top";
  },
  sftp: () => toggleSftp(),
};

function setFont(delta) {
  const tab = state.active;
  tab.fontSize = Math.max(8, Math.min(32, tab.fontSize + delta));
  tab.term.options.fontSize = tab.fontSize;
  tab.fit.fit();
}

// ---------- search ----------

function openSearch() {
  if (!state.active) return;
  $("#searchbar").hidden = false;
  $("#search-input").focus();
  $("#search-input").select();
}

function closeSearch() {
  $("#searchbar").hidden = true;
  state.active?.search.clearDecorations();
}

function runSearch(backwards) {
  const term = $("#search-input").value;
  if (!term || !state.active) return;
  const opts = { decorations: { matchOverviewRuler: "#e5a83a", activeMatchColorOverviewRuler: "#5b9dff" } };
  const found = backwards ? state.active.search.findPrevious(term, opts) : state.active.search.findNext(term, opts);
  if (!found) toast("No match");
}

// ---------- SFTP panel ----------

function toggleSftp() {
  const tab = state.active;
  tab.sftpOpen = !tab.sftpOpen;
  $("#sftp").hidden = !tab.sftpOpen;
  renderToolbar();
  if (tab.sftpOpen) refreshSftp();
  requestAnimationFrame(() => tab.fit.fit());
}

function sftpUrl(tab, suffix = "", params = {}) {
  const query = new URLSearchParams(params).toString();
  return `/api/sessions/${tab.session}/sftp${suffix}${query ? "?" + query : ""}`;
}

function joinPath(dir, name) {
  return dir.endsWith("/") ? dir + name : `${dir}/${name}`;
}

function parentPath(path) {
  const trimmed = path.replace(/\/+$/, "");
  const index = trimmed.lastIndexOf("/");
  return index <= 0 ? "/" : trimmed.slice(0, index);
}

async function refreshSftp(path) {
  const tab = state.active;
  if (!tab?.session || !tab.caps.sftp) return;
  $("#sftp-info").textContent = "Loading…";
  try {
    const data = await api("GET", sftpUrl(tab, "", { path: path ?? tab.sftpPath ?? "." }));
    if (tab !== state.active) return;
    tab.sftpPath = data.path;
    $("#sftp-path").value = data.path;
    $("#sftp-list").replaceChildren(...data.entries.map((entry) => fileRow(tab, entry)));
    $("#sftp-info").textContent = `${data.entries.length} items`;
  } catch (e) {
    $("#sftp-info").textContent = e.message;
  }
}

function fileRow(tab, entry) {
  const path = joinPath(tab.sftpPath, entry.name);
  const open = () => {
    if (entry.dir) refreshSftp(path);
    else download(tab, path);
  };
  return el("div", {
    class: "file",
    title: `${entry.mode}  ${new Date(entry.mtime * 1000).toLocaleString()}`,
    tabindex: "0",
    ondblclick: open,
    onkeydown: (e) => {
      if (e.key === "Enter") open();
      else if (e.key === "Delete") sftpDelete(tab, path, entry);
      else if (e.key === "F2") sftpRename(tab, path, entry.name);
    },
    oncontextmenu: (e) => {
      e.preventDefault();
      showMenu(e.clientX, e.clientY, [
        [entry.dir ? "Open" : "Download", open],
        ["Rename…", () => sftpRename(tab, path, entry.name)],
        ["Permissions…", () => sftpChmod(tab, path, entry)],
        ["Delete", () => sftpDelete(tab, path, entry), "danger"],
      ]);
    },
  },
    icon(entry.dir ? "folder" : entry.link ? "link" : "file"),
    el("span", { class: "fname" }, entry.name),
    el("span", { class: "fsize" }, entry.dir ? "" : formatSize(entry.size)));
}

async function download(tab, path) {
  try {
    const { url } = await api("POST", sftpUrl(tab, "/ticket"), { path });
    // Single-use link, no secret in the download history. An <a download> never
    // navigates the app away, even when the server answers with an error.
    el("a", { href: url, download: "" }).click();
  } catch (e) { toast(e.message, true); }
}

async function sftpOp(tab, body, busy = "") {
  const panel = $("#sftp");
  if (busy) { $("#sftp-info").textContent = busy; panel.classList.add("sftp-busy"); }
  try {
    await api("POST", sftpUrl(tab, "/op"), body);
  } catch (e) { toast(e.message, true); }
  panel.classList.remove("sftp-busy");
  refreshSftp();
}

async function sftpRename(tab, path, name) {
  const to = await prompt({ title: "Rename", body: el("p", {}, `New name for ${name}`), input: {} });
  if (to && to !== name) sftpOp(tab, { op: "rename", path, to: joinPath(tab.sftpPath, to) });
}

async function sftpDelete(tab, path, entry) {
  const what = entry.dir ? `the folder ${entry.name} and everything in it` : entry.name;
  const ok = await prompt({ title: "Delete", body: el("p", {}, `Delete ${what}? This cannot be undone.`), okLabel: "Delete", danger: true });
  if (ok) sftpOp(tab, { op: "remove", path, recursive: entry.dir }, `Deleting ${entry.name}…`);
}

async function sftpChmod(tab, path, entry) {
  const mode = await prompt({
    title: "Permissions", body: el("p", {}, `New mode for ${entry.name} (now ${entry.mode}): octal like 644, or u+x, go-w`),
    input: {},
  });
  if (mode) sftpOp(tab, { op: "chmod", path, mode });
}

let upload = null;  // the running upload's XMLHttpRequest, so it can be cancelled
let uploading = false;  // from the conflict question to the end

async function sftpUpload(files) {
  const tab = state.active;
  if (!tab?.session || !files.length) return;
  if (uploading) { toast("An upload is already running: wait for it or cancel it", true); return; }
  uploading = true;
  try { await runUpload(tab, files); } finally { uploading = false; }
}

async function runUpload(tab, files) {
  const directory = tab.sftpPath;
  let overwrite = false;
  try {
    // Never replace a remote file silently: ask first, naming the files.
    const { existing } = await api("POST", sftpUrl(tab, "/check"), { path: directory, names: files.map((f) => f.name) });
    if (existing.length) {
      const list = existing.length > 5 ? `${existing.slice(0, 5).join(", ")} and ${existing.length - 5} more` : existing.join(", ");
      overwrite = await prompt({
        title: "Replace files?", body: el("p", {}, `${list} already exist${existing.length === 1 ? "s" : ""} in ${directory}.`),
        okLabel: "Replace", danger: true,
      });
      if (!overwrite) return;
    }
  } catch (e) { toast(e.message, true); return; }
  const form = new FormData();
  for (const file of files) form.append("file", file, file.name);
  const total = files.reduce((n, f) => n + f.size, 0);
  const status = $("#sftp-transfer");  // the listing (#sftp-info) may refresh meanwhile
  const cancel = el("button", { type: "button", onclick: () => upload?.abort() }, "Cancel");
  const label = el("span", {}, `Uploading ${files.length} file(s)…`);
  status.replaceChildren(label, " ", cancel);
  upload = new XMLHttpRequest();
  const done = new Promise((resolve) => {
    upload.upload.onprogress = (e) => {
      const size = e.lengthComputable ? e.total : total;
      label.textContent = e.loaded >= size
        ? `Finishing upload of ${files.length} file(s)…`  // sent; the server is still writing it
        : `Uploading ${files.length} file(s): ${formatSize(e.loaded)} of ${formatSize(size)} (${Math.floor((e.loaded / size) * 100)}%)`;
    };
    upload.onload = () => {
      let data = {};
      try { data = JSON.parse(upload.responseText); } catch { /* not JSON */ }
      if (upload.status === 200) toast(`Uploaded ${data.uploaded.length} file(s)`);
      else toast(`Upload failed: ${data.error || upload.statusText}`, true);
      resolve();
    };
    upload.onerror = () => { toast("Upload failed: connection lost", true); resolve(); };
    upload.onabort = () => { toast("Upload cancelled; a partly sent file may remain on the server", true); resolve(); };
  });
  upload.open("POST", sftpUrl(tab, "/upload", { path: directory, overwrite: overwrite ? "1" : "0" }));
  upload.setRequestHeader(AUTH_HEADER, secret);
  upload.send(form);
  await done;
  upload = null;
  status.replaceChildren();
  refreshSftp();
}

const sftpActions = {
  up: () => refreshSftp(parentPath(state.active.sftpPath || "/")),
  refresh: () => refreshSftp(),
  mkdir: async () => {
    const name = await prompt({ title: "New folder", body: el("p", {}, "Folder name"), input: {} });
    if (name) sftpOp(state.active, { op: "mkdir", path: joinPath(state.active.sftpPath, name) });
  },
};

// ---------- wiring ----------

// App shortcuts (keyName: Ctrl+Shift in the desktop window, Alt+Shift in a browser). True if handled.
function appShortcut(e) {
  const key = e.key.toLowerCase();
  if (key === "n") openEditor();
  else if (key === "w" && state.active) closeTab(state.active);
  else if (key === "b") toggleSidebar();
  else if ((key === "pagedown" || key === "pageup") && state.tabs.length > 1) {
    const i = state.tabs.indexOf(state.active) + (key === "pagedown" ? 1 : -1);
    activate(state.tabs[(i + state.tabs.length) % state.tabs.length]);
  } else return false;
  e.preventDefault();
  return true;
}

// The sidebar can be hidden for a wider terminal; on a narrow window it opens over the terminal.
function toggleSidebar(open) {
  const narrow = matchMedia("(max-width: 720px)").matches;
  const cls = narrow ? "sidebar-open" : "sidebar-hidden";
  const shown = narrow ? document.body.classList.contains(cls) : !document.body.classList.contains(cls);
  const want = open ?? !shown;
  document.body.classList.toggle(cls, narrow ? want : !want);
  $("#sidebar-toggle").setAttribute("aria-expanded", String(want));
  if (!narrow) store.set("sidebar-hidden", !want);
  if (want) $("#quick-input").focus();
  else state.active?.term.focus();
  requestAnimationFrame(() => state.active?.fit.fit());
}

function bindEvents() {
  document.body.classList.toggle("sidebar-hidden", store.get("sidebar-hidden", false));
  $("#sidebar-toggle").onclick = () => toggleSidebar();
  $("#sidebar-toggle").setAttribute("aria-expanded", String(!store.get("sidebar-hidden", false)
    && !matchMedia("(max-width: 720px)").matches));
  $("#sidebar-toggle").title = `Show or hide the profiles (${keyName("B")})`;
  $("#new-profile").title = `New profile (${keyName("N")})`;
  $("#new-key").textContent = keyName("N");
  $("#profile-list").addEventListener("keydown", profileListKey);
  $("#sb-forwards").onclick = () => {
    const tab = state.active;
    if (tab) toast(tab.forwards.map((f) => f.text).join("\n"), tab.forwardsFailed > 0);
  };
  $("#new-profile").onclick = () => openEditor();
  // In the desktop window there is no browser to open another window in.
  $(".sidebar-footer").hidden = desktop;
  for (const action of ["fullscreen", "ontop"]) $(`[data-action=${action}]`).hidden = !desktop;
  $("#new-window").onclick = async () => {
    try {
      const { url } = await api("POST", "/api/link");
      window.open(url, "_blank", "noopener");
    } catch (e) { toast(e.message, true); }
  };
  $("#filter").oninput = renderProfiles;
  $("#quick-connect").onsubmit = async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    if (!$("#quick-input").value.trim() || form.dataset.busy) return;
    form.dataset.busy = "1";  // Enter pressed twice must not open two sessions
    try {
      // The server applies the core rules: a saved name, bob@host, host:22, COM3@9600...
      const profile = await api("POST", "/api/target", { text: $("#quick-input").value });
      connect(profile, { saved: profile.saved });
      $("#quick-input").value = "";
    } catch (err) { toast(err.message, true); } finally { delete form.dataset.busy; }
  };

  $("#advanced-toggle").checked = store.get("advanced", false);
  document.body.classList.toggle("show-advanced", $("#advanced-toggle").checked);
  $("#advanced-toggle").onchange = (e) => {
    document.body.classList.toggle("show-advanced", e.target.checked);
    store.set("advanced", e.target.checked);
  };
  for (const button of document.querySelectorAll("#prompt [data-close]")) {
    button.onclick = () => button.closest("dialog").close(button.dataset.close);
  }
  $("#editor-cancel").onclick = cancelEditor;
  $("#editor").addEventListener("cancel", (e) => { e.preventDefault(); cancelEditor(); });  // Esc
  $("#editor-form").onsubmit = (e) => {
    const action = e.submitter?.value;
    if (action === "save" || action === "connect") {
      e.preventDefault();
      saveEditor(action === "connect");
    }
  };

  $("#toolbar").onclick = (e) => {
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (action && !e.target.closest("button").disabled) toolbarActions[action]();
  };
  $("#searchbar").onclick = (e) => {
    const action = e.target.closest("[data-search]")?.dataset.search;
    if (action === "close") { closeSearch(); state.active?.term.focus(); }
    else if (action) runSearch(action === "prev");
  };
  $("#search-input").onkeydown = (e) => {
    if (e.key === "Enter") runSearch(e.shiftKey);
    if (e.key === "Escape") { closeSearch(); state.active?.term.focus(); }
  };

  $("#sftp").querySelector("header").onclick = (e) => {
    const action = e.target.closest("[data-sftp]")?.dataset.sftp;
    if (action) sftpActions[action]();
  };
  $("#sftp-path").onkeydown = (e) => { if (e.key === "Enter") refreshSftp(e.target.value); };
  $("#sftp-upload").onchange = (e) => { sftpUpload([...e.target.files]); e.target.value = ""; };
  const drop = $("#sftp-list");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("dragging"); };
  drop.ondragleave = () => drop.classList.remove("dragging");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("dragging"); sftpUpload([...e.dataTransfer.files]); };

  document.addEventListener("click", (e) => {
    if (!e.target.closest("#menu")) $("#menu").hidden = true;
    // The narrow-window sidebar is an overlay: a click beside it closes it.
    if (document.body.classList.contains("sidebar-open") && !e.target.closest("#sidebar, #sidebar-toggle")) toggleSidebar(false);
  });
  document.addEventListener("keydown", (e) => {
    // Seen here first (capture), before a terminal: stopped, these keys never reach the remote.
    if (desktop && e.key === "F11") {
      e.preventDefault();
      e.stopPropagation();
      toolbarActions.fullscreen();
      return;
    }
    if (e.key === "Escape" && document.body.classList.contains("sidebar-open")) toggleSidebar(false);
    if (shortcut(e) && !$("dialog[open]") && appShortcut(e)) e.stopPropagation();
  }, true);
  window.addEventListener("beforeunload", (e) => {
    if (state.tabs.some((t) => t.status === "connected")) e.preventDefault();
  });
}

async function main() {
  bindEvents();
  await login();
  const { options, sections, defaults, themes } = await api("GET", "/api/options");
  Object.assign(state, { options, sections, defaults, themes });
  await loadProfiles();
  activate(null);
}

main().catch((e) => toast(e.message, true));
