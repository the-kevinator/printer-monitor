/* Light/dark theme. Loaded in <head>, before the stylesheet paints, so a page
   opened in dark mode never flashes white first. With no saved choice the
   page follows the operating system; the toggle records an explicit one. */

(function () {
  "use strict";

  var KEY = "pm.theme";
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try {
      var value = window.localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch (e) { return null; }
  }

  function effective() {
    return saved() || (media && media.matches ? "dark" : "light");
  }

  function apply(theme) {
    if (theme) root.setAttribute("data-theme", theme);
    else root.removeAttribute("data-theme");
    var dark = effective() === "dark";
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < buttons.length; i++) {
      var label = dark ? "Switch to light mode" : "Switch to dark mode";
      buttons[i].setAttribute("aria-pressed", String(dark));
      buttons[i].setAttribute("aria-label", label);
      buttons[i].title = label;
    }
  }

  apply(saved());

  document.addEventListener("DOMContentLoaded", function () {
    apply(saved());
    document.addEventListener("click", function (event) {
      if (!event.target.closest("[data-theme-toggle]")) return;
      var next = effective() === "dark" ? "light" : "dark";
      try { window.localStorage.setItem(KEY, next); } catch (e) { /* ignore */ }
      apply(next);
    });
    // Following the system: keep the button's label true if it changes.
    if (media && media.addEventListener) {
      media.addEventListener("change", function () { if (!saved()) apply(null); });
    }
  });
})();
