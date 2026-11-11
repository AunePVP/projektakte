/* Document viewer (Dokument-Betrachter): links with data-betrachter="/dokumente/<id>/ansicht…" open the
   document in a panel (#betrachter in base.html) instead of leaving the page. Without this script the
   links keep their href (the document page). No inline code: the CSP allows scripts from /static only.

   History: opening the panel adds one history entry for the same address ({bt: url}); the browser's back
   button closes the panel, forward opens it again. Changing the document inside the panel (previous /
   next, another TIFF page) replaces that entry. htmx restores its own entries on "popstate" by
   re-rendering the page; between the panel's entry and the page entry of the same address that is
   skipped, so the page (and the scroll position of the timeline) stays as it is. */
(function () {
  "use strict";

  var panel = null;      // #betrachter
  var opener = null;     // the link that opened the panel (focus goes back there)
  var openerKey = "";    // its data-betrachter value, to find it again after htmx replaced the list
  var list = [];         // the viewer addresses of the list the link belongs to (previous / next)
  var index = -1;
  var current = "";      // address shown in the panel
  var seq = 0;           // ignores answers of superseded requests
  var shownAt = here();  // the address the page shows (path + query), for the popstate rule below
  var guarded = false;

  function here() { return location.pathname + location.search; }
  function getPanel() { return panel || (panel = document.getElementById("betrachter")); }
  function isOpen() { var p = getPanel(); return !!p && !p.hidden; }
  function ownEntry(state) { return !!(state && state.bt); }

  function viewerLinks(scope) {
    var out = [], seen = {};
    var links = scope.querySelectorAll("a[data-betrachter]");
    for (var i = 0; i < links.length; i++) {
      var key = links[i].getAttribute("data-betrachter");
      var doc = key.split("?")[0];
      if (out.length && seen[doc] === out.length - 1) { continue; }  // the same document twice in a row
      seen[doc] = out.length;
      out.push(key);
    }
    return out;
  }

  function load(url, onDone) {
    var p = getPanel();
    var mine = ++seq;
    p.setAttribute("aria-busy", "true");
    if (!isOpen()) {
      p.innerHTML = '<div class="bt"><p class="muted bt-laden">Dokument wird geladen …</p></div>';
    }
    fetch(url, { headers: { "HX-Request": "true" }, credentials: "same-origin" }).then(function (resp) {
      if (resp.status === 401 && resp.headers.get("HX-Redirect")) {
        location.href = "/login?next=" + encodeURIComponent(here());
        return null;
      }
      return resp.text().then(function (text) { return { ok: resp.ok, text: text }; });
    }).then(function (res) {
      if (!res || mine !== seq) { return; }
      p.removeAttribute("aria-busy");
      if (res.ok) {
        p.innerHTML = res.text;
      } else {
        var docPage = url.split("/ansicht")[0];
        p.innerHTML = '<div class="bt"><div class="bt-head"><div class="bt-titel"><h2 id="bt-titel" tabindex="-1">' +
          "Dokument kann nicht angezeigt werden</h2></div>" +
          '<button type="button" class="secondary bt-close" data-bt-schliessen aria-label="Betrachter schließen (Esc)">✕</button>' +
          '</div><p class="note warn">Bitte die <a href="' + docPage + '">Dokumentseite</a> öffnen.</p></div>';
      }
      current = url;
      showNav();
      onDone && onDone();
      var target = p.querySelector("[data-bt-ziel]");
      var el = target && document.getElementById(target.getAttribute("data-bt-ziel"));
      if (el) { el.classList.add("bt-ziel"); el.scrollIntoView({ block: "start" }); }
      var title = document.getElementById("bt-titel");
      if (title) { title.focus({ preventScroll: true }); }
    }).catch(function () {
      if (mine !== seq) { return; }
      p.removeAttribute("aria-busy");
      p.innerHTML = '<div class="bt"><div class="bt-head"><div class="bt-titel"><h2 id="bt-titel" tabindex="-1">Keine Verbindung' +
        '</h2></div><button type="button" class="secondary bt-close" data-bt-schliessen aria-label="Betrachter schließen (Esc)">✕</button></div></div>';
    });
  }

  function showNav() {
    var nav = getPanel().querySelector("[data-bt-nav]");
    if (!nav) { return; }
    if (list.length < 2 || index < 0) { nav.hidden = true; return; }
    nav.hidden = false;
    nav.querySelector("[data-bt-pos]").textContent = (index + 1) + " von " + list.length;
    nav.querySelector('[data-bt-schritt="-1"]').disabled = index <= 0;
    nav.querySelector('[data-bt-schritt="1"]').disabled = index >= list.length - 1;
  }

  function open(url, link) {
    guardHtmxHistory();
    if (link) {
      opener = link;
      openerKey = url;
      var scope = link.closest("[data-bt-liste], table, ol, ul");
      list = scope ? viewerLinks(scope) : [url];
      index = list.indexOf(url);
    }
    var wasOpen = isOpen();
    load(url);
    show();
    if (wasOpen && ownEntry(history.state)) {
      history.replaceState({ bt: url }, "", location.href);
    } else {
      history.pushState({ bt: url }, "", location.href);
    }
    onOwn = true;
    shownAt = here();
  }

  function show() {
    var p = getPanel();
    p.hidden = false;
    document.documentElement.classList.add("bt-offen");
  }

  function hide() {
    var p = getPanel();
    if (!p || p.hidden) { return; }
    seq++;
    p.hidden = true;
    p.innerHTML = "";  // also stops a PDF or a large image
    current = "";
    document.documentElement.classList.remove("bt-offen");
    var back = opener && document.contains(opener) ? opener : null;
    if (!back && openerKey) {
      back = document.querySelector('a[data-betrachter="' + openerKey.replace(/"/g, '\\"') + '"]');
    }
    if (back) { back.focus({ preventScroll: true }); }
  }

  function close() {
    if (ownEntry(history.state)) {
      history.back();  // the popstate below hides the panel
    } else {
      hide();
    }
  }

  function step(delta) {
    var next = index + delta;
    if (next < 0 || next >= list.length) { return; }
    index = next;
    load(list[index]);
    history.replaceState({ bt: list[index] }, "", location.href);
  }

  /* htmx restores its history entries by re-rendering the page. Between the panel's entry and the page
     entry of the same address there is nothing to restore: skip htmx there (window.onpopstate is htmx's
     handler; it is wrapped once, when the panel is first used). */
  function guardHtmxHistory() {
    if (guarded || typeof window.onpopstate !== "function") { return; }
    guarded = true;
    var htmxPop = window.onpopstate;
    window.onpopstate = function (event) {
      if (decide(event)) { return undefined; }
      return htmxPop.call(window, event);
    };
  }

  /* Whether a history step only opens or closes the panel (same address, the panel's entry on one side).
     Decided once per event – the order of htmx's handler and the listener below is not fixed. */
  var decided = null, skip = false, onOwn = false;
  function decide(event) {
    if (decided !== event) {
      decided = event;
      skip = here() === shownAt && (ownEntry(event.state) || onOwn);
      onOwn = ownEntry(event.state);
      shownAt = here();
    }
    return skip;
  }

  window.addEventListener("popstate", function (event) {
    decide(event);
    if (ownEntry(event.state)) {
      if (event.state.bt !== current) {
        list = [event.state.bt]; index = 0;
        load(event.state.bt);
        show();
      }
    } else {
      hide();
    }
  });

  // htmx changes the address (folder, tab, timeline): the panel closes, its entry stays behind
  document.addEventListener("htmx:beforeHistorySave", function () { if (isOpen()) { hide(); } });
  document.addEventListener("htmx:pushedIntoHistory", function () { shownAt = here(); onOwn = false; });
  document.addEventListener("htmx:replacedInHistory", function () { shownAt = here(); });

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey ||
        event.altKey) { return; }
    var target = event.target;
    if (!(target instanceof Element)) { return; }
    var link = target.closest("a[data-betrachter]");
    if (link && getPanel()) {
      event.preventDefault();
      open(link.getAttribute("data-betrachter"), link);
      return;
    }
    var inPanel = getPanel() && getPanel().contains(target);
    if (!inPanel) { return; }
    if (target.closest("[data-bt-schliessen]")) { event.preventDefault(); close(); return; }
    var stepper = target.closest("[data-bt-schritt]");
    if (stepper) { event.preventDefault(); step(parseInt(stepper.getAttribute("data-bt-schritt"), 10)); return; }
    var other = target.closest("a[data-bt-wechsel]");
    if (other) {
      event.preventDefault();
      var url = other.getAttribute("href");
      load(url);
      history.replaceState({ bt: url }, "", location.href);
      return;
    }
    var zoom = target.closest("[data-bt-zoom]");
    if (zoom) {
      var area = zoom.closest("[data-bt-zoombereich]");
      if (area) { area.classList.toggle("gross"); }
    }
  });

  // images on the document page (without the panel) zoom the same way
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof Element) || (getPanel() && getPanel().contains(target))) { return; }
    var zoom = target.closest(".bt-inline [data-bt-zoom]");
    if (zoom) { zoom.closest("[data-bt-zoombereich]").classList.toggle("gross"); }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen()) {
      event.preventDefault();
      close();
    }
  });
})();
