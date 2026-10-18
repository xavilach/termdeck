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
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
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

const THEMES = {
  dark: { background: "#0f1218", foreground: "#e4e7ee", cursor: "#5b9dff", selectionBackground: "#5b9dff55" },
  light: {
    background: "#ffffff", foreground: "#1b1f27", cursor: "#2f6fe0", selectionBackground: "#2f6fe033",
    black: "#1b1f27", white: "#8b93a5", brightWhite: "#667085", yellow: "#a06b00", brightYellow: "#8a5d00",
  },
  solarized: {
    background: "#002b36", foreground: "#839496", cursor: "#93a1a1", selectionBackground: "#073642",
    black: "#073642", red: "#dc322f", green: "#859900", yellow: "#b58900", blue: "#268bd2",
    magenta: "#d33682", cyan: "#2aa198", white: "#eee8d5", brightBlack: "#002b36", brightRed: "#cb4b16",
    brightGreen: "#586e75", brightYellow: "#657b83", brightBlue: "#839496", brightMagenta: "#6c71c4",
    brightCyan: "#93a1a1", brightWhite: "#fdf6e3",
  },
  dracula: {
    background: "#282a36", foreground: "#f8f8f2", cursor: "#f8f8f2", selectionBackground: "#44475a",
    black: "#21222c", red: "#ff5555", green: "#50fa7b", yellow: "#f1fa8c", blue: "#bd93f9",
    magenta: "#ff79c6", cyan: "#8be9fd", white: "#f8f8f2", brightBlack: "#6272a4", brightRed: "#ff6e6e",
    brightGreen: "#69ff94", brightYellow: "#ffffa5", brightBlue: "#d6acff", brightMagenta: "#ff92df",
    brightCyan: "#a4ffff", brightWhite: "#ffffff",
  },
};

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

function profileItem(p) {
  return el("div", {
    class: "profile",
    title: `${p.summary}\nDouble-click to connect`,
    ondblclick: () => connect(p),
    onclick: (e) => { if (e.detail === 1 && window.matchMedia("(pointer: coarse)").matches) connect(p); },
    oncontextmenu: (e) => { e.preventDefault(); profileMenu(p, e.clientX, e.clientY); },
  },
    el("span", { class: `kind ${p.kind}` }, p.kind === "ssh" ? "SSH" : "COM"),
    el("span", { class: "name" }, p.name),
    el("button", {
      class: "more", title: "Actions",
      onclick: (e) => { e.stopPropagation(); const r = e.target.getBoundingClientRect(); profileMenu(p, r.left, r.bottom); },
    }, "⋯"),
    el("span", { class: "summary" }, p.summary),
  );
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
  list.replaceChildren(...names.flatMap((g) => [
    g ? el("div", { class: "group-title" }, g) : (names.length > 1 ? el("div", { class: "group-title" }, "Other") : null),
    ...groups.get(g).map(profileItem),
  ]));
  if (!state.profiles.length) {
    list.append(el("p", { class: "muted", style: "padding: 8px" }, "No profiles yet. Use ＋ or quick connect."));
  }
  $("#welcome-cards").replaceChildren(...state.profiles.slice(0, 12).map((p) =>
    el("div", { class: "card", onclick: () => connect(p) },
      el("div", {}, p.name), el("div", { class: "summary" }, p.summary))));
}

function showMenu(x, y, items) {
  const menu = $("#menu");
  menu.replaceChildren(...items.map(([label, action, cls]) =>
    el("button", { class: cls, onclick: () => { menu.hidden = true; action(); } }, label)));
  menu.hidden = false;
  menu.style.left = `${Math.min(x, innerWidth - menu.offsetWidth - 8)}px`;
  menu.style.top = `${Math.min(y, innerHeight - menu.offsetHeight - 8)}px`;
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
  if (!confirm(`Delete profile "${p.name}"?`)) return;
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

function fieldRow(opt, values) {
  const [id, input] = fieldInput(opt, values[opt.key], (v) => {
    values[opt.key] = v;
    if (opt.key === "kind") renderEditor();
  });
  if (opt.widget === "device") attachPortSuggestions(input);
  const wide = opt.wide;
  return el("label", {
    class: ["field", opt.type === "bool" && "bool", wide && "wide", opt.advanced && "advanced"].filter(Boolean).join(" "),
    for: id,
  },
    el("span", {}, opt.label, opt.advanced ? el("span", { class: "adv-badge" }, "adv") : null),
    input,
    opt.help && opt.type !== "bool" ? el("small", {}, opt.help) : null,
    opt.help && opt.type === "bool" ? el("small", { style: "flex-basis:100%" }, opt.help) : null,
  );
}

let portCache = null;
async function attachPortSuggestions(input) {
  const list = el("datalist", { id: "serial-ports" });
  input.setAttribute("list", "serial-ports");
  input.after(list);
  try {
    portCache ??= await api("GET", "/api/serial-ports");
    list.replaceChildren(...portCache.map((p) =>
      el("option", { value: p.device }, [p.description, p.usb_id].filter(Boolean).join(" · "))));
  } catch { /* suggestions are optional */ }
}

function section(title, rows, extraClass = "") {
  rows = rows.filter(Boolean);
  if (!rows.length) return null;
  const allAdvanced = rows.every((r) => r.classList.contains("advanced"));
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
        }, "＋ Add forward")),
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
  const body = $("#editor-body");
  body.replaceChildren(...[
    section("General", o.profile.map((opt) => fieldRow(opt, v))),
    section(kind === "ssh" ? "SSH connection" : "Serial port", conn.filter((opt) => !opt.advanced).map((opt) => fieldRow(opt, v[kind]))),
    kind === "ssh" ? el("div", { class: "section" }, el("h3", {}, "Port forwards"), forwardsEditor(v.ssh.forwards)) : null,
    section(kind === "ssh" ? "SSH (advanced)" : "Serial line (advanced)", conn.filter((opt) => opt.advanced).map((opt) => fieldRow(opt, v[kind]))),
    section("Terminal", o.terminal.filter(applies).map((opt) => fieldRow(opt, v.terminal))),
  ].filter(Boolean));
}

function openEditor(profile = null, { isNew = !profile } = {}) {
  editing = { original: isNew ? null : profile.name, values: profile ? withDefaults(structuredClone(profile)) : blankProfile() };
  $("#editor-title").textContent = isNew ? "New profile" : `Edit ${profile.name}`;
  $("#editor-error").hidden = true;
  renderEditor();
  $("#editor").showModal();
  $("#editor-body").querySelector("input")?.focus();
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
    $("#editor").close();
    await loadProfiles();
    if (connectAfter) connect(saved);
  } catch (e) {
    const box = $("#editor-error");
    box.textContent = e.message;
    box.hidden = false;
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
  const tab = {
    id: ++tabIds,
    profile: withDefaults(profile),
    saved,
    status: "connecting",
    ws: null,
    session: null,
    canBreak: false,
    hasSftp: false,
    sftpPath: null,
    sftpOpen: false,
    fontSize: profile.terminal?.font_size || 14,
  };
  const t = tab.profile.terminal;
  tab.term = new Terminal({
    fontFamily: getComputedStyle(document.body).getPropertyValue("--mono"),
    fontSize: tab.fontSize,
    scrollback: t.scrollback,
    theme: THEMES[t.theme] || THEMES.dark,
    cursorBlink: true,
    allowProposedApi: true,
    rightClickSelectsWord: false,
  });
  tab.fit = new FitAddon.FitAddon();
  tab.search = new SearchAddon.SearchAddon();
  tab.term.loadAddon(tab.fit);
  tab.term.loadAddon(tab.search);
  tab.term.loadAddon(new WebLinksAddon.WebLinksAddon());
  tab.term.loadAddon(new Unicode11Addon.Unicode11Addon());
  tab.term.unicode.activeVersion = "11";

  tab.container = el("div", { class: "term", style: `background:${(THEMES[t.theme] || THEMES.dark).background}` });
  $("#terminals").append(tab.container);
  tab.term.open(tab.container);
  setupClipboard(tab);
  tab.term.onData((data) => send(tab, { type: "input", data }));
  tab.term.onResize(({ cols, rows }) => { send(tab, { type: "resize", cols, rows }); updateStatusbar(); });
  tab.resizer = new ResizeObserver(() => { if (!tab.container.hidden) tab.fit.fit(); });
  tab.resizer.observe(tab.container);

  state.tabs.push(tab);
  activate(tab);
  openSocket(tab);
  return tab;
}

function setupClipboard(tab) {
  const term = tab.term;
  // PuTTY habits: selecting copies, right click pastes. Ctrl+Shift+C/V also work.
  term.onSelectionChange(() => {
    const text = term.getSelection();
    if (text) navigator.clipboard?.writeText(text).catch(() => {});
  });
  const paste = () => navigator.clipboard?.readText().then((text) => text && term.paste(text)).catch(() => toast("Clipboard not available", true));
  tab.container.addEventListener("contextmenu", (e) => { e.preventDefault(); paste(); });
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown" || !e.ctrlKey || !e.shiftKey) return true;
    const key = e.key.toLowerCase();
    if (key === "c") { navigator.clipboard?.writeText(term.getSelection()); return false; }
    if (key === "v") { paste(); return false; }
    if (key === "f") { openSearch(); return false; }
    return true;
  });
}

function send(tab, message) {
  if (tab.ws?.readyState === WebSocket.OPEN) tab.ws.send(JSON.stringify(message));
}

function openSocket(tab) {
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
    } else if (m.type === "connected") {
      Object.assign(tab, { session: m.session, canBreak: m.can_break, hasSftp: m.sftp, sftpPath: null });
      setStatus(tab, "connected");
      tab.term.focus();
      if (tab.sftpOpen) refreshSftp();
    } else if (m.type === "error" || m.type === "closed") {
      tab.ended = m.message;
      if (m.type === "error") tab.term.write(`\r\n\x1b[31m${m.message}\x1b[0m\r\n`);
    }
  };
  ws.onclose = () => {
    if (tab.ws !== ws) return;
    tab.session = null;
    setStatus(tab, "closed");
    showReconnect(tab, tab.ended || "Disconnected");
    tab.ended = null;
  };
}

function showReconnect(tab, message) {
  tab.overlay?.remove();
  tab.overlay = el("div", { class: "overlay" },
    el("span", {}, message),
    el("button", { class: "primary", onclick: () => openSocket(tab) }, "Reconnect"),
    el("button", { onclick: () => closeTab(tab) }, "Close tab"));
  tab.container.append(tab.overlay);
  tab.reconnectKey = tab.term.onKey(({ domEvent }) => {
    if (tab.status === "closed" && domEvent.key === "Enter") openSocket(tab);
  });
}

function setStatus(tab, status) {
  tab.status = status;
  renderTabs();
  if (tab === state.active) renderToolbar();
}

function activate(tab) {
  state.active = tab;
  for (const t of state.tabs) t.container.hidden = t !== tab;
  $("#welcome").hidden = Boolean(tab);
  $("#workspace").hidden = !tab;
  closeSearch();
  renderTabs();
  renderToolbar();
  if (tab) {
    requestAnimationFrame(() => { tab.fit.fit(); tab.term.focus(); });
    $("#sftp").hidden = !(tab.sftpOpen && tab.hasSftp);
    if (tab.sftpOpen) refreshSftp();
  }
}

function closeTab(tab) {
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
      class: `tab ${tab === state.active ? "active" : ""}`,
      title: tab.profile.name,
      onclick: () => activate(tab),
      onauxclick: (e) => { if (e.button === 1) closeTab(tab); },
    },
      el("span", { class: `dot ${tab.status}` }),
      el("span", { class: "label" }, tab.profile.name),
      el("button", { class: "close", title: "Close", onclick: (e) => { e.stopPropagation(); closeTab(tab); } }, "✕"))));
}

function summaryOf(p) {
  if (p.kind === "ssh") return `ssh ${p.ssh.username ? p.ssh.username + "@" : ""}${p.ssh.host}:${p.ssh.port}`;
  const s = p.serial;
  return `serial ${s.device} ${s.baudrate} ${s.bytesize}${s.parity}${s.stopbits}`;
}

function renderToolbar() {
  const tab = state.active;
  $("#toolbar").hidden = !tab;
  if (!tab) { updateStatusbar(); return; }
  $("#tb-status").textContent = tab.status;
  $("#tb-summary").textContent = summaryOf(tab.profile);
  const show = (action, visible) => { $(`[data-action=${action}]`).hidden = !visible; };
  // Only the controls that make sense for this connection type.
  show("sftp", tab.profile.kind === "ssh");
  show("break", tab.profile.kind === "serial");
  show("reconnect", tab.status === "closed");
  $("[data-action=sftp]").disabled = !tab.hasSftp || tab.status !== "connected";
  $("[data-action=sftp]").classList.toggle("on", tab.sftpOpen);
  $("[data-action=break]").disabled = !tab.canBreak || tab.status !== "connected";
  updateStatusbar();
}

function updateStatusbar() {
  const tab = state.active;
  $("#sb-left").textContent = tab ? `${tab.profile.name} — ${tab.status}` : `${state.profiles.length} profiles`;
  $("#sb-right").textContent = tab ? `${tab.term.cols}×${tab.term.rows}  ${tab.profile.terminal.encoding}` : "";
}

const toolbarActions = {
  reconnect: () => openSocket(state.active),
  break: () => { send(state.active, { type: "break" }); toast("Break sent"); },
  "font-up": () => setFont(1),
  "font-down": () => setFont(-1),
  search: () => openSearch(),
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
  if (!tab?.session || !tab.hasSftp) return;
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
    ondblclick: open,
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
    el("span", {}, entry.dir ? "📁" : entry.link ? "🔗" : "📄"),
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

function bindEvents() {
  $("#new-profile").onclick = () => openEditor();
  // In the desktop window there is no browser to open another window in.
  $(".sidebar-footer").hidden = desktop;
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
  for (const button of document.querySelectorAll("dialog [data-close]")) {
    button.onclick = () => button.closest("dialog").close(button.dataset.close);
  }
  $("#editor-form").onsubmit = (e) => {
    const action = e.submitter?.value;
    if (action === "save" || action === "connect") {
      e.preventDefault();
      saveEditor(action === "connect");
    }
  };

  $("#toolbar").onclick = (e) => {
    const action = e.target.closest("[data-action]")?.dataset.action;
    if (action && !e.target.disabled) toolbarActions[action]();
  };
  $("#searchbar").onclick = (e) => {
    const action = e.target.dataset.search;
    if (action === "close") { closeSearch(); state.active?.term.focus(); }
    else if (action) runSearch(action === "prev");
  };
  $("#search-input").onkeydown = (e) => {
    if (e.key === "Enter") runSearch(e.shiftKey);
    if (e.key === "Escape") { closeSearch(); state.active?.term.focus(); }
  };

  $("#sftp").querySelector("header").onclick = (e) => {
    const action = e.target.dataset.sftp;
    if (action) sftpActions[action]();
  };
  $("#sftp-path").onkeydown = (e) => { if (e.key === "Enter") refreshSftp(e.target.value); };
  $("#sftp-upload").onchange = (e) => { sftpUpload([...e.target.files]); e.target.value = ""; };
  const drop = $("#sftp-list");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("dragging"); };
  drop.ondragleave = () => drop.classList.remove("dragging");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("dragging"); sftpUpload([...e.dataTransfer.files]); };

  document.addEventListener("click", (e) => { if (!e.target.closest("#menu")) $("#menu").hidden = true; });
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey && e.shiftKey)) return;
    const key = e.key.toLowerCase();
    if (key === "n") { e.preventDefault(); openEditor(); }
    else if (key === "w" && state.active) { e.preventDefault(); closeTab(state.active); }
    else if ((key === "pagedown" || key === "pageup") && state.tabs.length > 1) {
      e.preventDefault();
      const i = state.tabs.indexOf(state.active) + (key === "pagedown" ? 1 : -1);
      activate(state.tabs[(i + state.tabs.length) % state.tabs.length]);
    }
  }, true);
  window.addEventListener("beforeunload", (e) => {
    if (state.tabs.some((t) => t.status === "connected")) e.preventDefault();
  });
}

async function main() {
  bindEvents();
  await login();
  const { options, defaults } = await api("GET", "/api/options");
  Object.assign(state, { options, defaults });
  await loadProfiles();
  activate(null);
}

main().catch((e) => toast(e.message, true));
