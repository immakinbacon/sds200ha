/* Shared helpers for the add-on's web UI.
 *
 * No build step and no framework, deliberately: the add-on image is an
 * Alpine base with python3 and ffmpeg, there is no node toolchain in it, and
 * nothing here earns one. Plain DOM, classic scripts loaded in order,
 * everything hung off one `SDS` global so the files can't collide.
 *
 * Every URL built here is RELATIVE. The page is served through Home
 * Assistant ingress under a per-session prefix
 * (/api/hassio_ingress/<token>/), so an absolute "/api/settings" would
 * address Home Assistant itself and 404.
 */

"use strict";

window.SDS = window.SDS || {};

(function (SDS) {
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /** Clear a node and append children -- textContent = "" is the only
   *  reliable way to drop listeners along with the nodes that owned them. */
  function fill(node, ...children) {
    node.textContent = "";
    children.filter(Boolean).forEach((child) => node.appendChild(child));
    return node;
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      /* Some endpoints answer with a plain-text error body; fall through to
         the status check below, which produces a better message than a JSON
         parse error would. */
    }
    if (!response.ok) {
      const detail = (data && (data.detail || (data.errors || []).join(" "))) || `HTTP ${response.status}`;
      throw new Error(detail);
    }
    return data;
  }

  function post(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  /* Absolute ws:// URL for a path relative to this page -- the one place
     something absolute has to be built, since WebSocket() takes no relative
     URLs. The ingress prefix isn't known to this page as a constant, it's
     whatever path it was served under, so derive the base from that rather
     than from "/", which would be Home Assistant's own root. */
  function wsUrl(path) {
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const base = location.pathname.replace(/[^/]*$/, "");
    return `${scheme}//${location.host}${base}${path}`;
  }

  /* Mirrors config_store.slugify -- shown live so a name that collides with
     another scanner's id, or has no usable id at all, is visible before the
     save is rejected for it. */
  function slugify(name) {
    return name
      .toLowerCase()
      .replace(/[^a-z0-9]/g, "_")
      .replace(/^_+|_+$/g, "");
  }

  function formatTime(epochSeconds) {
    if (!epochSeconds) return "";
    const date = new Date(epochSeconds * 1000);
    const today = new Date();
    const time = date.toLocaleTimeString(undefined, { hour12: false });
    const sameDay =
      date.getDate() === today.getDate() &&
      date.getMonth() === today.getMonth() &&
      date.getFullYear() === today.getFullYear();
    return sameDay ? time : `${date.toLocaleDateString()} ${time}`;
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return "";
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${Math.round(seconds % 60)}s`;
  }

  function formatFrequency(mhz) {
    return mhz === null || mhz === undefined ? "" : `${Number(mhz).toFixed(4)} MHz`;
  }

  /** Debounce trailing-edge: used for the search box and the volume/squelch
   *  sliders, where firing on every input event would mean a request per
   *  pixel dragged. */
  function debounce(fn, delay) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), delay);
    };
  }

  SDS.el = el;
  SDS.fill = fill;
  SDS.api = api;
  SDS.post = post;
  SDS.wsUrl = wsUrl;
  SDS.slugify = slugify;
  SDS.formatTime = formatTime;
  SDS.formatDuration = formatDuration;
  SDS.formatFrequency = formatFrequency;
  SDS.debounce = debounce;
})(window.SDS);
