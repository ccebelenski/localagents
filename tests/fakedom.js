// A small DOM stand-in so the page scripts' pure parts can run under node in a test (3.11a).
// It holds what h(), dataTable and the renderers touch: elements with attributes, classes,
// text, children and listeners; text nodes; a selector matcher for compound selectors
// (tag, .class, [attr], [attr="v"]) joined by descendant spaces. Not a browser: layout,
// styles and focus semantics beyond `document.activeElement` are absent.
"use strict";

function Text(text) { this.nodeType = 3; this.data = String(text); this.parentNode = null; }
Object.defineProperty(Text.prototype, "textContent", { get() { return this.data; } });

function El(tag) {
  this.nodeType = 1;
  this.tagName = String(tag).toUpperCase();
  this.attrs = {};
  this.childNodes = [];
  this.parentNode = null;
  this.listeners = {};
  this._value = null;
  this.style = {};
  this.dataset = {};
}
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
El.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
Object.defineProperty(El.prototype, "className", {
  get() { return this.getAttribute("class") || ""; },
  set(v) { this.setAttribute("class", v); },
});
Object.defineProperty(El.prototype, "classList", {
  get() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (c) => list().indexOf(c) >= 0,
      add: (c) => { if (list().indexOf(c) < 0) el.className = list().concat([c]).join(" "); },
      remove: (c) => { el.className = list().filter((x) => x !== c).join(" "); },
      get length() { return list().length; },
    };
  },
});
Object.defineProperty(El.prototype, "children", { get() { return this.childNodes.filter((n) => n.nodeType === 1); } });
Object.defineProperty(El.prototype, "textContent", {
  get() { return this.childNodes.map((n) => n.textContent).join(""); },
  set(v) { this.childNodes = []; if (v != null && v !== "") this.appendChild(new Text(v)); },
});
Object.defineProperty(El.prototype, "value", {
  get() { return this._value != null ? this._value : (this.getAttribute("value") || ""); },
  set(v) { this._value = String(v); },
});
// 3.12b: the browser's isConnected — a walk up parentNode to document.body or documentElement.
// The table helper's level-1 skip reads it: a container discarded by a page change must force a
// full build rather than be patched in no page.
Object.defineProperty(El.prototype, "isConnected", {
  get() {
    let p = this;
    while (p.parentNode) p = p.parentNode;
    return p === global.document.body || p === global.document.documentElement;
  },
});
El.prototype.appendChild = function (n) {
  if (n.parentNode) n.parentNode.childNodes = n.parentNode.childNodes.filter((c) => c !== n);
  n.parentNode = this;
  this.childNodes.push(n);
  return n;
};
// 3.12b: insertBefore (the body reconcile moves kept trs) — same shape as appendChild,
// inserting before the reference child (null ref appends)
El.prototype.insertBefore = function (n, ref) {
  if (n.parentNode) n.parentNode.childNodes = n.parentNode.childNodes.filter((c) => c !== n);
  n.parentNode = this;
  const at = ref == null ? this.childNodes.length : this.childNodes.indexOf(ref);
  if (at < 0) this.childNodes.push(n); else this.childNodes.splice(at, 0, n);
  return n;
};
El.prototype.replaceChildren = function () {
  this.childNodes.forEach((n) => { n.parentNode = null; });
  this.childNodes = [];
  for (const n of arguments) {
    // as the browser: a non-Node argument is converted to a string and inserted as text, so a
    // null prints "null" (the 3.10c fold the operator saw under every requests table)
    this.appendChild(n != null && n.nodeType ? n : new Text(String(n)));
  }
};
El.prototype.remove = function () { if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter((c) => c !== this); this.parentNode = null; };
El.prototype.addEventListener = function (type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); };
El.prototype.removeEventListener = function (type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); };
// 3.12b: a caller may preset ev.target (the focusout deferral test dispatches the row's
// focusout with the select as its target, as the browser bubbles it); an unset target is the
// dispatching element, as before
El.prototype.dispatchEvent = function (ev) { if (ev.target == null) ev.target = this; (this.listeners[ev.type] || []).slice().forEach((fn) => fn.call(this, ev)); return true; };
El.prototype.click = function () { this.dispatchEvent(new Event("click")); };
El.prototype.focus = function () { global.document.activeElement = this; };
El.prototype.setSelectionRange = function () {};
El.prototype.getBoundingClientRect = function () { return { width: 0, height: 0, left: 0, top: 0, right: 0, bottom: 0 }; };

function parseCompound(s) {
  const out = { tag: null, classes: [], attrs: [] };
  const re = /^([a-zA-Z][\w-]*)|\.([\w-]+)|\[([\w-]+)(?:="((?:[^"\\]|\\.)*)")?\]/g;
  let m, pos = 0;
  while ((m = re.exec(s)) !== null) {
    if (m.index !== pos) throw new Error("fakedom: unsupported selector " + s);
    pos = re.lastIndex;
    if (m[1]) out.tag = m[1].toUpperCase();
    else if (m[2]) out.classes.push(m[2]);
    else out.attrs.push({ name: m[3], value: m[4] === undefined ? null : m[4].replace(/\\(.)/g, "$1") });
    if (m[0] === "") break;
  }
  if (pos !== s.length) throw new Error("fakedom: unsupported selector " + s);
  return out;
}
function matchesCompound(el, c) {
  if (c.tag && el.tagName !== c.tag) return false;
  for (const k of c.classes) if (!el.classList.contains(k)) return false;
  for (const a of c.attrs) {
    if (!el.hasAttribute(a.name)) return false;
    if (a.value !== null && el.getAttribute(a.name) !== a.value) return false;
  }
  return true;
}
function matches(el, parts) {
  if (!matchesCompound(el, parts[parts.length - 1])) return false;
  let i = parts.length - 2, p = el.parentNode;
  while (i >= 0 && p) {
    if (p.nodeType === 1 && matchesCompound(p, parts[i])) i--;
    p = p.parentNode;
  }
  return i < 0;
}
function descendants(el, out) {
  for (const n of el.childNodes) if (n.nodeType === 1) { out.push(n); descendants(n, out); }
  return out;
}
El.prototype.querySelectorAll = function (sel) {
  const groups = sel.split(",").map((g) => g.trim().split(/\s+/).map(parseCompound));
  return descendants(this, []).filter((el) => groups.some((parts) => matches(el, parts)));
};
El.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };

class Event { constructor(type) { this.type = type; } }

function install() {
  const body = new El("body");
  const document = {
    readyState: "loading", // the page's start() waits for DOMContentLoaded, which never comes
    activeElement: null,
    body: body,
    documentElement: new El("html"),
    createElement: (t) => new El(t),
    createElementNS: (ns, t) => new El(t),
    createTextNode: (t) => new Text(t),
    getElementById: () => null,
    querySelector: (s) => body.querySelector(s),
    querySelectorAll: (s) => body.querySelectorAll(s),
    addEventListener: () => {},
  };
  global.document = document;
  global.Event = Event;
  global.window = global;
  global.window.location = { hash: "" };
  global.window.localStorage = { getItem: () => null, setItem: () => {} };
  global.window.addEventListener = () => {};
  return document; // node has its own (read-only) navigator: the page reads it only on a click
}

module.exports = { install, El, Text, Event };
