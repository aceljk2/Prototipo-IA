/* Modo claro/oscuro: sigue al sistema hasta que la persona elige con el botón.
   La elección se recuerda en este navegador (localStorage, con respaldo si no está disponible). */
(function () {
  var root = document.documentElement, KEY = "peerflow-theme";
  var mq = window.matchMedia("(prefers-color-scheme: dark)");
  function saved() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function save(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  function current() { return root.getAttribute("data-theme") || (mq.matches ? "dark" : "light"); }

  var s = saved();
  if (s === "dark" || s === "light") root.setAttribute("data-theme", s);   // antes de pintar: sin parpadeo

  document.addEventListener("DOMContentLoaded", function () {
    var b = document.getElementById("theme-toggle");
    if (!b) return;
    function sync() { b.setAttribute("aria-pressed", current() === "dark" ? "true" : "false"); }
    b.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next); save(next); sync();
    });
    if (mq.addEventListener) mq.addEventListener("change", sync);
    sync();
  });
})();
