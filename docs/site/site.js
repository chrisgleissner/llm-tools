/* Theme toggle, tabs, copy buttons, hero video, full-demo dialog, nav highlight.
   Everything degrades to a working static page when this file does not run. */
(function () {
  "use strict";
  var root = document.documentElement;
  var KEY = "llm-tools-theme";
  var mqReduce = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  function theme() { return root.getAttribute("data-theme") === "light" ? "light" : "dark"; }
  function reduced() { return !!(mqReduce && mqReduce.matches); }

  /* ---------- theme ---------- */
  var toggle = document.getElementById("theme-toggle");
  function labelToggle() {
    if (!toggle) { return; }
    var next = theme() === "dark" ? "light" : "dark";
    toggle.setAttribute("aria-label", "Switch to " + next + " theme");
    toggle.setAttribute("title", "Switch to " + next + " theme");
  }
  if (toggle) {
    labelToggle();
    toggle.addEventListener("click", function () {
      var next = theme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* storage unavailable */ }
      labelToggle();
      syncVideos();
      syncDemoLink();
    });
  }

  /* ---------- header border once scrolled ---------- */
  var header = document.querySelector(".site-header");
  function onScroll() { if (header) { header.classList.toggle("scrolled", window.scrollY > 8); } }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  /* ---------- current section in the nav ---------- */
  var links = {};
  Array.prototype.forEach.call(document.querySelectorAll(".site-nav .links a"), function (a) {
    links[a.getAttribute("href").slice(1)] = a;
  });
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        var a = links[en.target.id];
        if (!a) { return; }
        if (en.isIntersecting) {
          Object.keys(links).forEach(function (k) { links[k].removeAttribute("aria-current"); });
          a.setAttribute("aria-current", "true");
        } else {
          a.removeAttribute("aria-current");
        }
      });
    }, { rootMargin: "-40% 0px -55% 0px" });
    Object.keys(links).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { io.observe(el); }
    });
  }

  /* ---------- hero video ---------- */
  /* The clip sits below the first screen. Its sources are chosen here (not every browser honours
     <source media>), nothing is fetched until the visible theme's clip nears the viewport, and the
     other theme's clip is never fetched. WCAG 2.2.2: a Pause button, and reduced motion keeps the
     clip paused on its poster. */
  var wraps = document.querySelectorAll(".hero-video-wrap");
  var toggleBtn = document.getElementById("vid-toggle");
  var mqNarrow = window.matchMedia ? window.matchMedia("(max-width: 600px)") : null;

  function visibleWrap() {
    for (var i = 0; i < wraps.length; i++) { if (wraps[i].getClientRects().length > 0) { return wraps[i]; } }
    return null;
  }
  function labelVideoToggle() {
    var w = visibleWrap();
    if (!toggleBtn || !w) { return; }
    var v = w.querySelector("video");
    var paused = v.paused && !w._parked;
    toggleBtn.querySelector("span").textContent = paused ? "Play" : "Pause";
    toggleBtn.setAttribute("aria-label", paused ? "Play the demo" : "Pause the demo");
    toggleBtn.querySelector("svg").innerHTML = paused
      ? '<path d="M2 1v10l9-5z"/>'
      : '<rect x="1.5" y="1" width="3" height="10" rx="1"/><rect x="7.5" y="1" width="3" height="10" rx="1"/>';
  }
  function setState(w, state) { w.setAttribute("data-state", state); labelVideoToggle(); }

  /* Keep the sources whose media query matches, in the order of the markup (MP4 before WebM). */
  function chooseSources(v) {
    if (!v._sources) { v._sources = Array.prototype.slice.call(v.querySelectorAll("source")); }
    Array.prototype.slice.call(v.querySelectorAll("source")).forEach(function (s) { v.removeChild(s); });
    v._sources.forEach(function (s) {
      var media = s.getAttribute("media");
      if (media && window.matchMedia && !window.matchMedia(media).matches) { return; }
      v.appendChild(s);
    });
  }

  /* The poster is fetched only for the visible theme's clip, once it is near the viewport. */
  function showPoster(v) {
    var narrow = !!(mqNarrow && mqNarrow.matches);
    v._posterOn = true;
    v.poster = (narrow && v.getAttribute("data-poster-mobile")) || v.getAttribute("data-poster") || "";
  }

  function play(w) {
    var v = w.querySelector("video");
    w._parked = false;
    if (v.preload !== "auto") { v.preload = "auto"; }
    var p = v.play();
    if (p && p.catch) { p.catch(function () { setState(w, "paused"); }); }
  }

  function syncVideos() {
    Array.prototype.forEach.call(wraps, function (w) {
      var v = w.querySelector("video");
      if (w.getClientRects().length === 0) { w._parked = true; try { v.pause(); } catch (e) { /* ignore */ } return; }
      if (w._near && !v._posterOn) { showPoster(v); }
      if (reduced() || w.getAttribute("data-user") === "paused") {
        try { v.pause(); } catch (e) { /* ignore */ }
        setState(w, "paused");
      } else if (w._near && (v.paused || w._parked)) {
        play(w);
      } else if (!w._near && !v.paused) {
        w._parked = true;  /* scrolled away: stop decoding, keep the picture, no play overlay */
        v.pause();
      }
    });
    labelVideoToggle();
  }

  var io = null;
  if ("IntersectionObserver" in window) {
    io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { en.target._near = en.isIntersecting; });
      syncVideos();
    }, { rootMargin: "300px 0px" });
  }

  Array.prototype.forEach.call(wraps, function (w) {
    var v = w.querySelector("video");
    var overlay = w.querySelector(".play-overlay");
    var bar = w.parentNode.querySelector(".vid-progress");
    var last = 0;
    v.removeAttribute("controls");
    v.removeAttribute("autoplay");
    chooseSources(v);
    w._near = !io;
    if (io) { io.observe(w); }
    if (reduced()) { try { v.pause(); } catch (e) { /* ignore */ } setState(w, "paused"); }
    v.addEventListener("playing", function () { w._parked = false; setState(w, "playing"); });
    v.addEventListener("pause", function () { if (!v.ended && !w._parked) { setState(w, "paused"); } });
    if (bar) {
      v.addEventListener("timeupdate", function () {
        if (!(v.duration > 0)) { return; }
        var p = v.currentTime / v.duration;
        bar.classList.toggle("rewind", p < last);
        bar.style.setProperty("--p", p.toFixed(4));
        last = p;
      });
    }
    if (overlay) {
      overlay.addEventListener("click", function () {
        w.removeAttribute("data-user");
        play(w);
      });
    }
  });
  if (toggleBtn) {
    toggleBtn.addEventListener("click", function () {
      var w = visibleWrap();
      if (!w) { return; }
      var v = w.querySelector("video");
      if (v.paused || w._parked) {
        w.removeAttribute("data-user");
        play(w);
      } else {
        w.setAttribute("data-user", "paused");
        v.pause();
      }
    });
  }
  if (mqReduce) {
    var onReduce = function () { syncVideos(); };
    if (mqReduce.addEventListener) { mqReduce.addEventListener("change", onReduce); }
    else if (mqReduce.addListener) { mqReduce.addListener(onReduce); }
  }
  syncVideos();

  /* Narrow screens get the portrait clip and poster. */
  Array.prototype.forEach.call(document.querySelectorAll("video[data-poster-mobile]"), function (v) {
    var onChange = function () {
      var loaded = v.preload === "auto";
      var wasPlaying = !v.paused;
      if (v._posterOn) { showPoster(v); }
      chooseSources(v);
      if (loaded) { v.load(); if (wasPlaying) { var p = v.play(); if (p && p.catch) { p.catch(function () { /* ignore */ }); } } }
    };
    if (mqNarrow) {
      if (mqNarrow.addEventListener) { mqNarrow.addEventListener("change", onChange); }
      else if (mqNarrow.addListener) { mqNarrow.addListener(onChange); }
    }
  });

  /* ---------- full demo dialog ---------- */
  var demoLink = document.getElementById("full-demo");
  var dlg = document.getElementById("demo-dialog");
  function syncDemoLink() {
    if (!demoLink) { return; }
    var src = demoLink.getAttribute(theme() === "light" ? "data-light" : "data-dark");
    if (src) { demoLink.setAttribute("href", src); }
  }
  syncDemoLink();
  if (demoLink && dlg && typeof dlg.showModal === "function") {
    var dv = dlg.querySelector("video");
    demoLink.addEventListener("click", function (e) {
      e.preventDefault();
      /* Pause the hero loop while the full run plays. */
      Array.prototype.forEach.call(wraps, function (w) { try { w.querySelector("video").pause(); } catch (err) { /* ignore */ } });
      dv.setAttribute("src", demoLink.getAttribute("href"));
      dlg.showModal();
      var p = dv.play();
      if (p && p.catch) { p.catch(function () { /* controls stay available */ }); }
    });
    var closeBtn = dlg.querySelector("[data-close]");
    if (closeBtn) { closeBtn.addEventListener("click", function () { dlg.close(); }); }
    dlg.addEventListener("click", function (e) { if (e.target === dlg) { dlg.close(); } });
    dlg.addEventListener("close", function () {
      dv.pause();
      dv.removeAttribute("src");
      dv.load();
      syncVideos();
    });
  }

  /* ---------- tabs (install method, plan/run/review) ---------- */
  Array.prototype.forEach.call(document.querySelectorAll("[data-tabs]"), function (group) {
    var tabs = Array.prototype.slice.call(group.querySelectorAll('[role="tab"]'));
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.setAttribute("tabindex", on ? "0" : "-1");
        var panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) { panel.hidden = !on; }
      });
      if (focus) { tab.focus(); }
    }
    tabs.forEach(function (t, i) {
      t.addEventListener("click", function () { select(t, false); });
      t.addEventListener("keydown", function (e) {
        var n = null;
        if (e.key === "ArrowRight") { n = tabs[(i + 1) % tabs.length]; }
        else if (e.key === "ArrowLeft") { n = tabs[(i - 1 + tabs.length) % tabs.length]; }
        else if (e.key === "Home") { n = tabs[0]; }
        else if (e.key === "End") { n = tabs[tabs.length - 1]; }
        if (n) { e.preventDefault(); select(n, true); }
      });
    });
    select(tabs[0], false);
  });

  /* ---------- copy buttons ---------- */
  var status = document.getElementById("copy-status");
  function announce(msg) { if (status) { status.textContent = msg; } }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  Array.prototype.forEach.call(document.querySelectorAll("button.copy"), function (btn) {
    btn.addEventListener("click", function () {
      var target = document.getElementById(btn.getAttribute("data-copy"));
      if (!target) { return; }
      var clone = target.cloneNode(true);
      Array.prototype.forEach.call(clone.querySelectorAll(".prompt"), function (n) { n.parentNode.removeChild(n); });
      var lines = clone.querySelectorAll(".ln");
      var text = lines.length
        ? Array.prototype.map.call(lines, function (n) { return n.textContent.trim(); }).join("\n")
        : clone.textContent.trim();
      var done = function (ok) {
        var label = btn.querySelector(".copy-label");
        btn.setAttribute("data-state", ok ? "copied" : "failed");
        if (label) { label.textContent = ok ? "Copied" : "Copy failed"; }
        announce(ok ? "Command copied to clipboard" : "Copy failed; select the command and copy it manually");
        window.setTimeout(function () {
          btn.removeAttribute("data-state");
          if (label) { label.textContent = "Copy"; }
        }, 2000);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(fallbackCopy(text)); });
      } else {
        done(fallbackCopy(text));
      }
    });
  });
})();
