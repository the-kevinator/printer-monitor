/* Printer Monitor dashboard.
   Polls /api/printers, renders cards or a table, and drives discovery. */

(function () {
  "use strict";

  var REFRESH_MS = 15000;
  var SORT_KEY_STORE = "pm.sortKey";
  var SORT_DIR_STORE = "pm.sortDir";
  /* Set by the server template; non-empty when the app is mounted under
     an IIS virtual application path such as "/printers". */
  var BASE = (document.body && document.body.dataset.base) || "";
  var DATA = (document.body && document.body.dataset) || {};
  var state = {
    auth: {
      signedIn: DATA.signedIn === "true",
      username: DATA.username || null,
      unconfigured: DATA.unconfigured === "true"
    },
    subnets: null,          // the /api/subnets payload, once loaded
    accounts: null,         // the /api/account payload, once loaded
    printers: [],
    summary: null,
    poller: null,
    lowThreshold: 20,
    filter: null,          // health state to show, or null for all
    query: "",
    view: "cards",
    sortKey: "ip",
    sortDir: 1,
    loaded: false,
    error: null
  };

  var el = {};

  // ------------------------------------------------------------ utilities

  function $(id) { return document.getElementById(id); }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function num(value) {
    if (value == null || value === "") return "—";
    return Number(value).toLocaleString();
  }

  function relTime(seconds) {
    if (!seconds) return "never";
    var delta = Date.now() / 1000 - seconds;
    if (delta < 0) delta = 0;
    if (delta < 45) return "just now";
    if (delta < 90) return "1 min ago";
    if (delta < 3600) return Math.round(delta / 60) + " min ago";
    if (delta < 7200) return "1 hr ago";
    if (delta < 86400) return Math.round(delta / 3600) + " hr ago";
    return Math.round(delta / 86400) + " d ago";
  }

  function duration(seconds) {
    if (seconds == null) return "—";
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var mins = Math.floor((seconds % 3600) / 60);
    if (days) return days + "d " + hours + "h";
    if (hours) return hours + "h " + mins + "m";
    return mins + "m";
  }

  /* Human labels for hrPrinterDetectedErrorState bit names. */
  var ERROR_LABELS = {
    lowPaper: "Paper low",
    noPaper: "Out of paper",
    lowToner: "Toner low",
    noToner: "Out of toner",
    doorOpen: "Door open",
    jammed: "Paper jam",
    offline: "Offline",
    serviceRequested: "Service required",
    inputTrayMissing: "Input tray missing",
    outputTrayMissing: "Output tray missing",
    markerSupplyMissing: "Supply missing",
    outputNearFull: "Output bin nearly full",
    outputFull: "Output bin full",
    inputTrayEmpty: "Input tray empty",
    overduePreventMaint: "Maintenance overdue"
  };
  var SOFT_ERRORS = { lowPaper: 1, lowToner: 1, outputNearFull: 1, overduePreventMaint: 1 };

  var ICONS = {
    ok: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.5 4.5L6 12 2.5 8.5"/></svg>',
    warning: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 2.2l6 11.3H2z"/><path d="M8 6.6v3.1"/><path d="M8 11.8h.01"/></svg>',
    critical: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.2"/><path d="M8 5v3.4"/><path d="M8 10.8h.01"/></svg>',
    offline: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.2"/><path d="M3.6 3.6l8.8 8.8"/></svg>',
    unknown: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.2"/><path d="M6.4 6.2A1.7 1.7 0 019.6 7c0 1.1-1.6 1.3-1.6 2.3"/><path d="M8 11.6h.01"/></svg>'
  };

  var HEALTH_LABELS = {
    ok: "Ready", warning: "Attention", critical: "Critical",
    offline: "Offline", unknown: "Unknown"
  };

  /* Status is never colour alone: every pill carries an icon and a word. */
  function statusPill(health) {
    var key = HEALTH_LABELS[health] ? health : "unknown";
    return '<span class="status status-' + key + '">' + ICONS[key] +
      "<span>" + HEALTH_LABELS[key] + "</span></span>";
  }

  function chipClass(colorant) {
    switch (colorant) {
      case "black": return "chip-black";
      case "cyan": return "chip-cyan";
      case "magenta": return "chip-magenta";
      case "yellow": return "chip-yellow";
      default: return "chip-other";
    }
  }

  function supplyLabel(supply) {
    if (supply.colorant) {
      return supply.colorant.charAt(0).toUpperCase() + supply.colorant.slice(1);
    }
    var byType = {
      wasteToner: "Waste toner", opc: "Drum", developer: "Developer",
      fuser: "Fuser", transferUnit: "Transfer", cleanerUnit: "Cleaner",
      wasteInk: "Waste ink", staples: "Staples", wastePaper: "Waste"
    };
    if (byType[supply.type]) return byType[supply.type];
    return supply.description || supply.type || "Supply";
  }

  /* One meter row. The fill carries severity; the chip carries cartridge
     identity; the printed percentage carries the value. */
  function meterRow(supply) {
    var stateClass = "s-unknown";
    if (supply.state === "ok") stateClass = "s-ok";
    else if (supply.state === "low") stateClass = "s-low";
    else if (supply.state === "empty") stateClass = "s-empty";

    var pct = supply.percent;
    var hasPct = pct != null;
    var width = hasPct ? Math.max(0, Math.min(100, pct)) : 0;
    var valueText, valueClass = "meter-value";
    if (hasPct) {
      valueText = pct + "%";
    } else if (supply.state === "some_remaining") {
      valueText = "OK";
      valueClass += " is-unknown";
    } else {
      valueText = "n/a";
      valueClass += " is-unknown";
    }

    var label = supplyLabel(supply);
    var title = (supply.description || label) +
      (supply.is_receptacle ? " — " + width + "% full" : "") +
      (supply.unit && supply.unit !== "percent" ? " (unit: " + supply.unit + ")" : "");

    return '<div class="meter-row" title="' + esc(title) + '">' +
      '<span class="chip ' + chipClass(supply.colorant) + '" aria-hidden="true"></span>' +
      '<span class="meter-label">' + esc(label) + "</span>" +
      '<span class="track ' + stateClass + '" role="img" aria-label="' +
        esc(label + ": " + valueText) + '">' +
        '<span class="fill" data-full="' + (width >= 99.5) + '" style="width:' + width + '%"></span>' +
      "</span>" +
      '<span class="' + valueClass + '">' + esc(valueText) + "</span>" +
      "</div>";
  }

  function alertBadges(errors) {
    if (!errors || !errors.length) return "";
    return '<div class="alerts">' + errors.map(function (name) {
      var soft = SOFT_ERRORS[name];
      return '<span class="alert' + (soft ? " is-soft" : "") + '">' +
        (soft ? ICONS.warning : ICONS.critical) +
        esc(ERROR_LABELS[name] || name) + "</span>";
    }).join("") + "</div>";
  }

  // ------------------------------------------------------------- filtering

  function visiblePrinters() {
    var q = state.query.trim().toLowerCase();
    return state.printers.filter(function (p) {
      if (state.filter && p.health !== state.filter) return false;
      if (!q) return true;
      var hay = [p.display_name, p.ip, p.hostname, p.model, p.vendor,
                 p.serial_number, p.location, p.sys_name, p.notes]
        .filter(Boolean).join(" ").toLowerCase();
      return hay.indexOf(q) !== -1;
    });
  }

  // --------------------------------------------------------------- render

  function renderTiles() {
    if (!state.summary) return;
    var by = state.summary.by_health || {};
    var tiles = [
      { key: null, label: "Printers", value: state.summary.total, cls: "" },
      { key: "ok", label: "Ready", value: by.ok || 0, cls: "is-good" },
      { key: "warning", label: "Attention", value: by.warning || 0, cls: "is-warning" },
      { key: "critical", label: "Critical", value: by.critical || 0, cls: "is-critical" },
      { key: "offline", label: "Offline", value: by.offline || 0, cls: "is-offline" }
    ];
    el.tiles.innerHTML = tiles.map(function (t) {
      var isActive = state.filter === t.key;
      return '<button class="tile ' + t.cls + '" type="button" data-filter="' +
        (t.key == null ? "" : t.key) + '" aria-pressed="' + (isActive ? "true" : "false") + '">' +
        '<span class="label">' + (t.key ? ICONS[t.key] : "") + esc(t.label) + "</span>" +
        '<span class="value">' + num(t.value) + "</span></button>";
    }).join("");
  }

  function renderCard(p) {
    var s = p.summary || {};
    var supplies = (s.supplies || []);
    var toners = supplies.filter(function (x) { return x.is_toner; });
    var others = supplies.filter(function (x) { return !x.is_toner; });
    var shown = toners.concat(others.slice(0, Math.max(0, 4 - toners.length)));

    var metersHtml = shown.length
      ? '<div class="meters">' + shown.map(meterRow).join("") + "</div>"
      : '<div class="meta-note">No supply data reported' +
        (p.online ? "" : " — printer is not responding") + "</div>";

    var subParts = [];
    if (p.model && p.model !== p.display_name) subParts.push(esc(p.model));
    else if (p.vendor) subParts.push(esc(p.vendor));
    if (p.location) subParts.push(esc(p.location));

    var figs = [];
    if (s.page_count != null) figs.push({ k: "Pages", v: num(s.page_count) });
    if (s.printer_status) figs.push({ k: "State", v: esc(s.printer_status) });
    if (p.stats && p.stats.uptime_seconds != null) {
      figs.push({ k: "Uptime", v: duration(p.stats.uptime_seconds) });
    }
    figs.push({ k: "Polled", v: relTime(s.polled_at || p.last_polled) });

    var webUrl = p.web_url || ("http://" + p.ip);

    return '<article class="card is-' + esc(p.health) +
        (p.enabled ? "" : " is-disabled") + '" data-ip="' + esc(p.ip) + '">' +
      '<div class="card-head">' +
        '<div class="card-title">' +
          "<h3><a href=\"" + BASE + "/printer/" + esc(p.ip) + "\">" + esc(p.display_name) + "</a></h3>" +
          '<div class="card-sub"><span class="ip">' + esc(p.ip) + "</span>" +
            (subParts.length ? '<span class="dot">·</span>' + subParts.join('<span class="dot">·</span>') : "") +
          "</div>" +
        "</div>" +
        statusPill(p.health) +
      "</div>" +
      '<div class="card-body">' +
        metersHtml +
        alertBadges(s.errors) +
        (s.console_text ? '<div class="console">' + esc(s.console_text) + "</div>" : "") +
        (s.error && !p.online ? '<div class="meta-note">' + esc(s.error) + "</div>" : "") +
        '<div class="figs">' + figs.map(function (f) {
          return '<div class="fig"><div class="k">' + f.k + '</div><div class="v">' + f.v + "</div></div>";
        }).join("") + "</div>" +
      "</div>" +
      '<div class="card-foot">' +
        '<a class="btn btn-sm btn-primary" href="' + esc(webUrl) + '" target="_blank" rel="noopener noreferrer">' +
          '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6.5 3H3.4A1.4 1.4 0 002 4.4v8.2A1.4 1.4 0 003.4 14h8.2a1.4 1.4 0 001.4-1.4V9.5"/><path d="M9.5 2H14v4.5"/><path d="M14 2L7.5 8.5"/></svg>' +
          "Web UI</a>" +
        '<a class="btn btn-sm" href="' + BASE + '/printer/' + esc(p.ip) + '">Details</a>' +
        '<span class="spacer"></span>' +
        '<button class="btn btn-sm" type="button" data-act="refresh" data-ip="' + esc(p.ip) + '" title="Poll this printer now">' +
          '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 8a6 6 0 11-1.8-4.3"/><path d="M14 2v3.6h-3.6"/></svg>' +
        "</button>" +
      "</div>" +
      "</article>";
  }

  var COLUMNS = [
    { key: "display_name", label: "Printer" },
    { key: "ip", label: "Address" },
    { key: "model", label: "Model" },
    { key: "health", label: "Status" },
    { key: "toner", label: "Toner" },
    { key: "page_count", label: "Pages", num: true },
    { key: "location", label: "Location" },
    { key: "last_polled", label: "Polled" }
  ];

  function sortValue(p, key) {
    var s = p.summary || {};
    switch (key) {
      case "ip":
        return p.ip.split(".").map(function (o) {
          return ("00" + o).slice(-3);
        }).join(".");
      case "page_count": return s.page_count == null ? -1 : s.page_count;
      case "toner":
        return s.lowest_toner_percent == null ? 999 : s.lowest_toner_percent;
      case "last_polled": return s.polled_at || p.last_polled || 0;
      case "health":
        return ({ critical: 0, offline: 1, warning: 2, unknown: 3, ok: 4 })[p.health];
      default:
        return String(p[key] == null ? "" : p[key]).toLowerCase();
    }
  }

  function renderTable(rows) {
    var head = COLUMNS.map(function (c) {
      var arrow = state.sortKey === c.key
        ? ' <span class="arrow">' + (state.sortDir > 0 ? "▲" : "▼") + "</span>" : "";
      return "<th data-sort=\"" + c.key + "\">" + c.label + arrow + "</th>";
    }).join("");

    var body = rows.map(function (p) {
      var s = p.summary || {};
      var toners = (s.supplies || []).filter(function (x) { return x.is_toner; });
      var mini = toners.length
        ? '<div class="toner-mini">' + toners.map(function (t) {
            var cls = t.state === "empty" ? "s-empty" : t.state === "low" ? "s-low"
              : t.state === "ok" ? "s-ok" : "s-unknown";
            var w = t.percent == null ? 0 : t.percent;
            return '<span class="m track ' + cls + '" title="' +
              esc(supplyLabel(t) + ": " + (t.percent == null ? "unknown" : t.percent + "%")) +
              '"><span class="fill" style="width:' + w + '%"></span></span>';
          }).join("") +
          '<span class="meter-value">' +
            (s.lowest_toner_percent == null ? "" : s.lowest_toner_percent + "%") +
          "</span></div>"
        : '<span class="meta-note">—</span>';

      return "<tr>" +
        '<td><a href="' + BASE + '/printer/' + esc(p.ip) + '">' + esc(p.display_name) + "</a></td>" +
        '<td><a class="mono" href="' + esc(p.web_url || "http://" + p.ip) +
          '" target="_blank" rel="noopener noreferrer">' + esc(p.ip) + "</a></td>" +
        "<td>" + esc(p.model || "—") + "</td>" +
        "<td>" + statusPill(p.health) + "</td>" +
        "<td>" + mini + "</td>" +
        '<td class="num">' + num(s.page_count) + "</td>" +
        "<td>" + esc(p.location || "—") + "</td>" +
        "<td>" + relTime(s.polled_at || p.last_polled) + "</td>" +
        "</tr>";
    }).join("");

    return '<div class="table-wrap"><table><thead><tr>' + head +
      "</tr></thead><tbody>" + body + "</tbody></table></div>";
  }

  function renderEmpty() {
    var discovering = state.poller && state.poller.discovery && state.poller.discovery.running;
    if (discovering) {
      return '<div class="empty">' +
        "<h2>Scanning the network…</h2>" +
        "<p>Looking for printers that answer SNMP on the configured subnets.</p></div>";
    }
    if (state.printers.length && (state.filter || state.query)) {
      return '<div class="empty"><h2>Nothing matches</h2>' +
        "<p>No printer matches the current filter or search.</p>" +
        '<button class="btn" type="button" data-act="clear-filters">Clear filters</button></div>';
    }
    return '<div class="empty">' +
      '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9V3h12v6"/><rect x="3" y="9" width="18" height="7" rx="2"/><path d="M6 16h12v5H6z"/></svg>' +
      "<h2>No printers yet</h2>" +
      "<p>Run a discovery sweep to find printers on your network, or add one by " +
      "its IP address if it sits on a different subnet.</p>" +
      '<button class="btn btn-primary" type="button" data-act="discover">Discover printers</button> ' +
      '<button class="btn" type="button" data-act="add">Add by IP</button></div>';
  }

  function render() {
    renderTiles();
    renderSortControls();

    var rows = visiblePrinters();
    rows.sort(function (a, b) {
      var av = sortValue(a, state.sortKey), bv = sortValue(b, state.sortKey);
      if (av < bv) return -state.sortDir;
      if (av > bv) return state.sortDir;
      return 0;
    });

    if (!state.loaded) {
      el.main.innerHTML = '<div class="empty"><h2>Loading…</h2></div>';
    } else if (!rows.length) {
      el.main.innerHTML = renderEmpty();
    } else if (state.view === "table") {
      el.main.innerHTML = renderTable(rows);
    } else {
      el.main.innerHTML = '<div class="grid">' + rows.map(renderCard).join("") + "</div>";
    }

    el.count.textContent = state.loaded
      ? rows.length + (rows.length === 1 ? " printer" : " printers") +
        (rows.length !== state.printers.length ? " of " + state.printers.length : "")
      : "";

    renderDiscovery();
  }

  function renderDiscovery() {
    var d = state.poller && state.poller.discovery;
    if (!d || (!d.running && !d.finished_at)) {
      el.discovery.hidden = true;
      return;
    }
    if (d.running) {
      el.discovery.hidden = false;
      el.discovery.className = "notice";
      el.discovery.innerHTML =
        '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M14 8a6 6 0 11-1.8-4.3"/></svg>' +
        "<div style=\"flex:1\"><strong>Scanning " + esc((d.subnets || []).join(", ")) +
        "</strong> — " + num(d.scanned) + " of " + num(d.total) +
        " addresses checked, " + num(d.found) + " printer(s) found." +
        '<div class="progress"><div style="width:' + (d.percent || 0) + '%"></div></div></div>' +
        '<button class="btn btn-sm" type="button" data-act="cancel-discovery">Stop</button>';
      el.discoverBtn.disabled = true;
      return;
    }
    el.discoverBtn.disabled = false;
    if (d.error) {
      el.discovery.hidden = false;
      el.discovery.className = "notice is-warn";
      el.discovery.innerHTML = ICONS.warning +
        "<div><strong>Discovery problem.</strong> " + esc(d.error) + "</div>";
      return;
    }
    el.discovery.hidden = true;
  }

  // ---------------------------------------------------------------- data

  /* The CSRF cookie is readable on purpose: every write echoes it back in a
     header, which no other origin can do. */
  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)pm_csrf=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : (DATA.csrf || "");
  }

  function api(path, options) {
    options = options || {};
    var headers = { "Content-Type": "application/json" };
    if (options.method && options.method !== "GET") {
      headers["X-PM-CSRF"] = csrfToken();
    }
    return fetch(BASE + path, Object.assign({}, options, {
      headers: Object.assign(headers, options.headers || {})
    })).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var err = new Error(body.detail || response.status + " " + response.statusText);
          err.status = response.status;
          throw err;
        }
        return body;
      });
    });
  }

  /* A write rejected for want of a session should send the person to the
     login page rather than pop an alert they cannot act on. */
  function handleWriteError(err, what) {
    if (err && err.status === 401) {
      window.location.href = BASE + "/login?next=" +
        encodeURIComponent(window.location.pathname + window.location.search);
      return;
    }
    window.alert(what + ": " + err.message);
  }

  function load() {
    return api("/api/printers").then(function (data) {
      state.printers = data.printers || [];
      state.summary = data.summary;
      state.poller = data.poller;
      state.lowThreshold = data.low_toner_threshold;
      if (data.auth) {
        var changed = state.auth.signedIn !== data.auth.signed_in ||
          state.auth.unconfigured !== data.auth.unconfigured;
        state.auth.signedIn = data.auth.signed_in;
        state.auth.username = data.auth.username;
        state.auth.unconfigured = data.auth.unconfigured;
        // A session that expired in a background tab should show as signed out
        // rather than leave the write buttons looking available.
        if (changed) renderAccount();
      }
      state.loaded = true;
      state.error = null;
      render();
    }).catch(function (err) {
      state.error = err.message;
      state.loaded = true;
      el.main.innerHTML = '<div class="notice is-warn">' + ICONS.warning +
        "<div><strong>Cannot reach the monitor service.</strong> " +
        esc(err.message) + "</div></div>";
    });
  }

  function pollDiscoveryUntilDone() {
    api("/api/discover").then(function (progress) {
      state.poller = state.poller || {};
      state.poller.discovery = progress;
      renderDiscovery();
      if (progress.running) {
        setTimeout(pollDiscoveryUntilDone, 1200);
      } else {
        load();
      }
    }).catch(function () { /* transient; the periodic refresh will recover */ });
  }


  // ------------------------------------------------------- subnet manager

  var SOURCE_LABELS = {
    config: "from config.json",
    env: "from the environment",
    autodetected: "guessed from this server",
    manual: ""
  };

  function subnetRow(entry, kind) {
    var problem = entry.valid ? "" :
      '<div class="subnet-problem">' + esc(entry.problem) + "</div>";
    var origin = SOURCE_LABELS[entry.source] || "";
    var meta = [];
    if (entry.valid) {
      meta.push(entry.host_count.toLocaleString() +
        (entry.host_count === 1 ? " address" : " addresses"));
    }
    if (origin) meta.push(origin);

    /* A range is switched off rather than deleted when it is temporarily not
       wanted, so the label explaining what it is survives. */
    var toggleTitle = entry.enabled ? "Stop scanning this range"
                                    : "Start scanning this range again";
    var scanBtn = (kind === "include" && entry.valid && entry.enabled)
      ? '<button class="btn btn-sm" type="button" data-sub-act="scan" data-id="' +
        esc(entry.id) + '" title="Sweep only this range now">Scan</button>'
      : "";

    return '<div class="subnet-row' + (entry.enabled ? "" : " is-off") +
        (entry.valid ? "" : " is-bad") + '" data-id="' + esc(entry.id) + '">' +
      '<label class="subnet-toggle" title="' + esc(toggleTitle) + '">' +
        '<input type="checkbox" data-sub-act="toggle" data-id="' + esc(entry.id) +
          '"' + (entry.enabled ? " checked" : "") + '>' +
        '<span class="sr-only">Scan ' + esc(entry.cidr) + "</span>" +
      "</label>" +
      '<div class="subnet-main">' +
        '<span class="subnet-cidr mono">' + esc(entry.cidr) + "</span>" +
        (entry.label ? '<span class="subnet-label">' + esc(entry.label) + "</span>" : "") +
        (meta.length ? '<span class="subnet-meta">' + esc(meta.join(" · ")) + "</span>" : "") +
        problem +
      "</div>" +
      scanBtn +
      '<button class="btn btn-sm btn-danger" type="button" data-sub-act="delete" ' +
        'data-id="' + esc(entry.id) + '" title="Remove this range">' +
        '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M3.4 3.4l9.2 9.2"/><path d="M12.6 3.4l-9.2 9.2"/></svg>' +
        '<span class="sr-only">Remove ' + esc(entry.cidr) + "</span>" +
      "</button>" +
      "</div>";
  }

  function renderSubnets() {
    var data = state.subnets;
    if (!data) return;

    var includes = data.subnets || [];
    var excludes = data.exclusions || [];

    el.subnetList.innerHTML = includes.length
      ? includes.map(function (e) { return subnetRow(e, "include"); }).join("")
      : '<p class="meta-note">No ranges yet. Add one below to let discovery ' +
        "find printers.</p>";
    el.excludeList.innerHTML = excludes.length
      ? excludes.map(function (e) { return subnetRow(e, "exclude"); }).join("")
      : '<p class="meta-note">Nothing excluded.</p>';

    var active = includes.filter(function (e) { return e.enabled && e.valid; });
    el.subnetCount.textContent = active.length
      ? active.length + (active.length === 1 ? " range, " : " ranges, ") +
        data.sweep_target_count.toLocaleString() + " addresses per sweep"
      : "nothing will be scanned";

    var notes = ["Saved in " + data.path + "."];
    if (!data.allow_public_subnets) {
      notes.push("Only internal ranges are accepted; set " +
        '"allow_public_subnets": true in config.json to lift that.');
    }
    notes.push("Each range is capped at " +
      data.max_hosts.toLocaleString() + " addresses.");
    el.subnetFootnote.textContent = notes.join(" ");

    /* Keep the header summary honest as soon as the list changes, rather than
       leaving it stale until the next full page load. */
    if (el.subnetSummary) {
      el.subnetSummary.textContent = active.length
        ? active.map(function (e) { return e.cidr; }).join(", ")
        : "no subnets configured";
    }
  }

  function subnetError(message) {
    if (!message) {
      el.subnetError.hidden = true;
      el.subnetError.textContent = "";
      return;
    }
    el.subnetError.hidden = false;
    el.subnetError.textContent = message;
  }

  function loadSubnets() {
    return api("/api/subnets").then(function (data) {
      state.subnets = data;
      renderSubnets();
    });
  }

  function applySubnetPayload(payload) {
    state.subnets = payload;
    renderSubnets();
    subnetError(null);
    load();  // the sweep footprint changed; refresh the dashboard's view of it
  }

  function openSubnets() {
    subnetError(null);
    loadSubnets().then(function () {
      $("subnet-dialog").showModal();
    }).catch(function (err) {
      window.alert("Could not load the subnet list: " + err.message);
    });
  }

  function addSubnet(kind, cidrInput, labelInput, form) {
    var cidr = cidrInput.value.trim();
    if (!cidr) return;
    var submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    api("/api/subnets", {
      method: "POST",
      body: JSON.stringify({
        cidr: cidr, kind: kind, label: labelInput.value.trim() || null
      })
    }).then(function (payload) {
      cidrInput.value = "";
      labelInput.value = "";
      applySubnetPayload(payload);
      cidrInput.focus();
    }).catch(function (err) {
      if (err.status === 401) { handleWriteError(err, ""); return; }
      subnetError(err.message);
    }).then(function () { submit.disabled = false; });
  }

  function wireSubnetDialog() {
    var dialog = $("subnet-dialog");

    dialog.addEventListener("click", function (event) {
      var target = event.target.closest("[data-sub-act]");
      if (!target) return;
      var action = target.getAttribute("data-sub-act");
      var id = target.getAttribute("data-id");

      if (action === "toggle") {
        api("/api/subnets/" + encodeURIComponent(id), {
          method: "PATCH",
          body: JSON.stringify({ enabled: target.checked })
        }).then(applySubnetPayload).catch(function (err) {
          target.checked = !target.checked;
          if (err.status === 401) { handleWriteError(err, ""); return; }
          subnetError(err.message);
        });
        return;
      }

      if (action === "delete") {
        var row = target.closest(".subnet-row");
        var cidr = row ? row.querySelector(".subnet-cidr").textContent : "this range";
        if (!window.confirm("Remove " + cidr + " from the list?\n\n" +
            "Printers already discovered in it stay in the inventory; it just " +
            "will not be swept again.")) {
          return;
        }
        api("/api/subnets/" + encodeURIComponent(id), { method: "DELETE" })
          .then(applySubnetPayload)
          .catch(function (err) {
            if (err.status === 401) { handleWriteError(err, ""); return; }
            subnetError(err.message);
          });
        return;
      }

      if (action === "scan") {
        target.disabled = true;
        api("/api/subnets/" + encodeURIComponent(id) + "/scan", { method: "POST" })
          .then(function () {
            dialog.close();
            pollDiscoveryUntilDone();
          })
          .catch(function (err) {
            if (err.status === 401) { handleWriteError(err, ""); return; }
            subnetError(err.message);
          })
          .then(function () { target.disabled = false; });
      }
    });

    $("subnet-add").addEventListener("submit", function (event) {
      event.preventDefault();
      addSubnet("include", $("subnet-cidr"), $("subnet-label"), event.target);
    });
    $("exclude-add").addEventListener("submit", function (event) {
      event.preventDefault();
      addSubnet("exclude", $("exclude-cidr"), $("exclude-label"), event.target);
    });
    $("subnet-close").addEventListener("click", function () { dialog.close(); });
  }

  // ------------------------------------------------------------- accounts

  function renderAccount() {
    if (!el.account) return;
    /* Managing accounts needs a session, and the list of who can sign in is
       not something to advertise to anonymous viewers. */
    if (el.accountsBtn) el.accountsBtn.hidden = !state.auth.signedIn;
    if (state.auth.unconfigured) {
      el.account.innerHTML =
        '<button class="btn btn-sm" type="button" data-act="setup-account">Set up login</button>';
      return;
    }
    if (state.auth.signedIn) {
      el.account.innerHTML =
        '<span class="account-name" title="Signed in">' +
          esc(state.auth.username) + "</span>" +
        '<form method="post" action="' + BASE + '/logout">' +
          '<button class="btn btn-sm" type="submit">Sign out</button>' +
        "</form>";
      return;
    }
    el.account.innerHTML =
      '<a class="btn btn-sm" href="' + BASE + '/login?next=' +
      encodeURIComponent(window.location.pathname) + '">Sign in</a>';
  }

  function wireSetupDialog() {
    var form = $("setup-form");
    if (!form) return;
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var pw = $("setup-password").value;
      var err = $("setup-error");
      if (pw !== $("setup-password2").value) {
        err.hidden = false;
        err.textContent = "The two passwords do not match.";
        return;
      }
      var submit = form.querySelector('button[type="submit"]');
      submit.disabled = true;
      api("/api/account/setup", {
        method: "POST",
        body: JSON.stringify({ username: $("setup-username").value, password: pw })
      }).then(function () {
        window.location.reload();
      }).catch(function (e) {
        err.hidden = false;
        err.textContent = e.message;
      }).then(function () { submit.disabled = false; });
    });
    $("setup-cancel").addEventListener("click", function () {
      $("setup-dialog").close();
    });
  }

  // ----------------------------------------------------- accounts manager

  /* The server refuses to delete the last account, because that would drop the
     dashboard back to open-to-everyone. Reflect that here rather than offering
     a button whose only outcome is an error. */
  function accountRow(user, isOnly, isSelf) {
    var meta = ["last sign-in " + relTime(user.last_login)];
    if (isOnly) meta.push("the only account");

    var remove = isOnly ? "" :
      '<button class="btn btn-sm btn-danger" type="button" data-acct-act="delete" ' +
        'data-username="' + esc(user.username) + '" title="Remove this account">' +
        '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M3.4 3.4l9.2 9.2"/><path d="M12.6 3.4l-9.2 9.2"/></svg>' +
        '<span class="sr-only">Remove ' + esc(user.username) + "</span>" +
      "</button>";

    return '<div class="subnet-row" data-username="' + esc(user.username) + '">' +
      '<div class="subnet-main">' +
        '<span class="subnet-cidr mono">' + esc(user.username) + "</span>" +
        (isSelf ? '<span class="account-badge">you</span>' : "") +
        '<span class="subnet-meta">' + esc(meta.join(" \u00b7 ")) + "</span>" +
      "</div>" +
      remove +
      "</div>";
  }

  function renderAccounts() {
    var data = state.accounts;
    if (!data) return;
    var users = data.users || [];
    var only = users.length === 1;

    el.accountList.innerHTML = users.length
      ? users.map(function (u) {
          return accountRow(u, only, u.username === state.auth.username);
        }).join("")
      : '<p class="meta-note">No accounts yet.</p>';

    el.accountCount.textContent = users.length
      ? users.length + (users.length === 1 ? " account" : " accounts")
      : "";
  }

  function accountError(message) {
    el.accountOk.hidden = true;
    if (!message) {
      el.accountError.hidden = true;
      el.accountError.textContent = "";
      return;
    }
    el.accountError.hidden = false;
    el.accountError.textContent = message;
  }

  function accountOk(message) {
    el.accountError.hidden = true;
    el.accountOk.hidden = false;
    el.accountOk.textContent = message;
  }

  function loadAccounts() {
    return api("/api/account").then(function (data) {
      state.accounts = data;
      renderAccounts();
    });
  }

  function openAccounts() {
    accountError(null);
    loadAccounts().then(function () {
      $("accounts-dialog").showModal();
    }).catch(function (err) {
      window.alert("Could not load the account list: " + err.message);
    });
  }

  function wireAccountDialog() {
    var dialog = $("accounts-dialog");
    if (!dialog) return;

    dialog.addEventListener("click", function (event) {
      var target = event.target.closest("[data-acct-act]");
      if (!target || target.getAttribute("data-acct-act") !== "delete") return;
      var username = target.getAttribute("data-username");
      var isSelf = username === state.auth.username;
      var warning = isSelf
        ? "Remove your own account, " + username + "?\n\n" +
          "You will be signed out immediately and will need another " +
          "account's password to make changes again."
        : "Remove the account " + username + "?\n\n" +
          "They will be signed out and will no longer be able to change anything.";
      if (!window.confirm(warning)) return;

      target.disabled = true;
      api("/api/account/users/" + encodeURIComponent(username), { method: "DELETE" })
        .then(function () {
          /* Deleting yourself invalidates the session this page is holding, so
             a reload is the only honest next state. */
          if (isSelf) { window.location.reload(); return; }
          accountOk("Removed " + username + ".");
          return loadAccounts();
        })
        .catch(function (err) {
          target.disabled = false;
          if (err.status === 401) { handleWriteError(err, ""); return; }
          accountError(err.message);
        });
    });

    var addForm = $("account-add");
    addForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var nameInput = $("account-username");
      var pwInput = $("account-password");
      var name = nameInput.value.trim();
      if (!name) return;
      var submit = addForm.querySelector('button[type="submit"]');
      submit.disabled = true;
      api("/api/account/users", {
        method: "POST",
        body: JSON.stringify({ username: name, password: pwInput.value })
      }).then(function (result) {
        nameInput.value = "";
        pwInput.value = "";
        accountOk("Added " + ((result.user && result.user.username) || name) + ".");
        nameInput.focus();
        return loadAccounts();
      }).catch(function (err) {
        if (err.status === 401) { handleWriteError(err, ""); return; }
        accountError(err.message);
      }).then(function () { submit.disabled = false; });
    });

    var pwForm = $("account-pw-form");
    pwForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var current = $("pw-current");
      var next = $("pw-new");
      var confirm = $("pw-confirm");
      if (next.value !== confirm.value) {
        accountError("The two new passwords do not match.");
        return;
      }
      var submit = pwForm.querySelector('button[type="submit"]');
      submit.disabled = true;
      api("/api/account/password", {
        method: "POST",
        body: JSON.stringify({
          current_password: current.value, new_password: next.value
        })
      }).then(function () {
        pwForm.reset();
        /* The server re-issues the session cookie, so we stay signed in. */
        accountOk("Your password has been changed. Other sessions signed out.");
        return loadAccounts();
      }).catch(function (err) {
        if (err.status === 401) { handleWriteError(err, ""); return; }
        accountError(err.message);
      }).then(function () { submit.disabled = false; });
    });

    $("account-close").addEventListener("click", function () { dialog.close(); });
  }

  // -------------------------------------------------------------- actions

  function startDiscovery() {
    el.discoverBtn.disabled = true;
    api("/api/discover", { method: "POST" }).then(function () {
      pollDiscoveryUntilDone();
    }).catch(function (err) {
      el.discoverBtn.disabled = false;
      handleWriteError(err, "Could not start discovery");
    });
  }

  function refreshOne(ip, button) {
    if (button) { button.disabled = true; }
    api("/api/printers/" + encodeURIComponent(ip) + "/refresh", { method: "POST" })
      .then(load)
      .catch(function (err) { handleWriteError(err, "Refresh failed"); })
      .then(function () { if (button) button.disabled = false; });
  }

  function setFilter(value) {
    state.filter = value || null;
    render();
  }

  function setView(view) {
    state.view = view;
    try { window.localStorage.setItem("pm.view", view); } catch (e) { /* ignore */ }
    el.viewCards.setAttribute("aria-pressed", String(view === "cards"));
    el.viewTable.setAttribute("aria-pressed", String(view === "table"));
    render();
  }

  /* Sorting is driven from the toolbar in both views, and additionally from
     the table's own column headers; all three paths land here. */

  function sortLabel(key) {
    for (var i = 0; i < COLUMNS.length; i++) {
      if (COLUMNS[i].key === key) return COLUMNS[i].label;
    }
    return key;
  }

  function setSort(key, dir) {
    state.sortKey = key;
    state.sortDir = dir < 0 ? -1 : 1;
    try {
      window.localStorage.setItem(SORT_KEY_STORE, state.sortKey);
      window.localStorage.setItem(SORT_DIR_STORE, String(state.sortDir));
    } catch (e) { /* ignore */ }
    render();
  }

  /* A fresh column starts ascending; the current one reverses. */
  function toggleSort(key) {
    setSort(key, state.sortKey === key ? -state.sortDir : 1);
  }

  function renderSortControls() {
    if (!el.sortKey) return;
    if (!el.sortKey.options.length) {
      el.sortKey.innerHTML = COLUMNS.map(function (c) {
        return '<option value="' + esc(c.key) + '">' + esc(c.label) + "</option>";
      }).join("");
    }
    el.sortKey.value = state.sortKey;
    var ascending = state.sortDir > 0;
    el.sortDir.textContent = ascending ? "\u25B2" : "\u25BC";
    el.sortDir.setAttribute("aria-pressed", String(!ascending));
    var title = (ascending ? "Ascending" : "Descending") + " by " +
      sortLabel(state.sortKey).toLowerCase() + " \u2014 click to reverse";
    el.sortDir.title = title;
    el.sortDir.setAttribute("aria-label", title);
  }

  // ------------------------------------------------------------------ init

  function wire() {
    el.tiles = $("tiles");
    el.main = $("main");
    el.count = $("count");
    el.discovery = $("discovery");
    el.discoverBtn = $("discover");
    el.viewCards = $("view-cards");
    el.viewTable = $("view-table");
    el.sortKey = $("sort-key");
    el.sortDir = $("sort-dir");
    el.account = $("account");
    el.subnetList = $("subnet-list");
    el.excludeList = $("exclude-list");
    el.subnetCount = $("subnet-count");
    el.subnetError = $("subnet-error");
    el.subnetFootnote = $("subnet-footnote");
    el.subnetSummary = $("subnet-summary");
    el.accountsBtn = $("edit-accounts");
    el.accountList = $("account-list");
    el.accountCount = $("account-count");
    el.accountError = $("account-error");
    el.accountOk = $("account-ok");

    el.discoverBtn.addEventListener("click", startDiscovery);
    $("poll-now").addEventListener("click", function () {
      api("/api/poll", { method: "POST" }).then(function () {
        setTimeout(load, 1500);
      }).catch(function (err) { handleWriteError(err, "Could not refresh"); });
    });
    $("add-printer").addEventListener("click", function () { $("add-dialog").showModal(); });
    $("edit-subnets").addEventListener("click", openSubnets);
    if (el.subnetSummary) el.subnetSummary.addEventListener("click", openSubnets);
    wireSubnetDialog();
    wireSetupDialog();
    wireAccountDialog();
    if (el.accountsBtn) el.accountsBtn.addEventListener("click", openAccounts);
    renderAccount();

    $("search").addEventListener("input", function (event) {
      state.query = event.target.value;
      render();
    });

    el.viewCards.addEventListener("click", function () { setView("cards"); });
    el.viewTable.addEventListener("click", function () { setView("table"); });

    el.sortKey.addEventListener("change", function (event) {
      setSort(event.target.value, state.sortDir);
    });
    el.sortDir.addEventListener("click", function () {
      setSort(state.sortKey, -state.sortDir);
    });

    el.tiles.addEventListener("click", function (event) {
      var tile = event.target.closest("[data-filter]");
      if (!tile) return;
      var value = tile.getAttribute("data-filter");
      setFilter(state.filter === value ? null : value);
    });

    el.main.addEventListener("click", function (event) {
      var act = event.target.closest("[data-act]");
      if (act) {
        var action = act.getAttribute("data-act");
        if (action === "refresh") { refreshOne(act.getAttribute("data-ip"), act); return; }
        if (action === "discover") { startDiscovery(); return; }
        if (action === "add") { $("add-dialog").showModal(); return; }
        if (action === "edit-subnets") { openSubnets(); return; }
        if (action === "setup-account") { $("setup-dialog").showModal(); return; }
        if (action === "cancel-discovery") {
          api("/api/discover/cancel", { method: "POST" });
          return;
        }
        if (action === "clear-filters") {
          state.filter = null; state.query = ""; $("search").value = ""; render(); return;
        }
      }
      var th = event.target.closest("th[data-sort]");
      if (th) toggleSort(th.getAttribute("data-sort"));
    });

    el.discovery.addEventListener("click", function (event) {
      if (event.target.closest('[data-act="cancel-discovery"]')) {
        api("/api/discover/cancel", { method: "POST" })
          .catch(function (err) { handleWriteError(err, "Could not stop the sweep"); });
      }
    });

    /* The setup and autodetect notices sit above the grid, outside el.main. */
    document.querySelector(".wrap").addEventListener("click", function (event) {
      var act = event.target.closest('[data-act="edit-subnets"], [data-act="setup-account"]');
      if (!act) return;
      if (act.getAttribute("data-act") === "edit-subnets") openSubnets();
      else $("setup-dialog").showModal();
    });

    var addForm = $("add-form");
    addForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var ip = $("add-ip").value.trim();
      if (!ip) return;
      var submit = addForm.querySelector('button[type="submit"]');
      submit.disabled = true;
      api("/api/printers", {
        method: "POST",
        body: JSON.stringify({
          ip: ip,
          label: $("add-label").value.trim() || null,
          community: $("add-community").value.trim() || null
        })
      }).then(function (result) {
        $("add-dialog").close();
        addForm.reset();
        if (!result.reachable) {
          window.alert("Added " + ip + ", but it did not answer SNMP. Check that " +
            "SNMP is enabled on the printer and the community string matches.");
        }
        return load();
      }).catch(function (err) {
        handleWriteError(err, "Could not add printer");
      }).then(function () { submit.disabled = false; });
    });

    $("add-cancel").addEventListener("click", function () { $("add-dialog").close(); });

    try {
      var saved = window.localStorage.getItem("pm.view");
      if (saved === "table" || saved === "cards") state.view = saved;
      var savedKey = window.localStorage.getItem(SORT_KEY_STORE);
      var known = COLUMNS.some(function (c) { return c.key === savedKey; });
      if (known) {
        state.sortKey = savedKey;
        state.sortDir = window.localStorage.getItem(SORT_DIR_STORE) === "-1" ? -1 : 1;
      }
    } catch (e) { /* ignore */ }
    el.viewCards.setAttribute("aria-pressed", String(state.view === "cards"));
    el.viewTable.setAttribute("aria-pressed", String(state.view === "table"));
  }

  document.addEventListener("DOMContentLoaded", function () {
    wire();
    load().then(function () {
      if (state.poller && state.poller.discovery && state.poller.discovery.running) {
        pollDiscoveryUntilDone();
      }
    });
    setInterval(function () {
      if (document.hidden) return;
      load();
    }, REFRESH_MS);
  });
})();
