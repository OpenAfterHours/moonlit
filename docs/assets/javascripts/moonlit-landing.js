// moonlit landing — standalone vanilla JS for the docs homepage.
// Renders deterministic stars and wires the copy-install buttons.
// Mirrors the algorithm in moonlit-shared.jsx so visuals match the design.

(function () {
  function rand(i, salt, seed) {
    var x = Math.sin((i + 1) * 12.9898 + salt * 78.233 + seed) * 43758.5453;
    return x - Math.floor(x);
  }

  function renderStars(host) {
    var count = parseInt(host.getAttribute("data-count") || "130", 10);
    var seed = parseInt(host.getAttribute("data-seed") || "2", 10);
    var frag = document.createDocumentFragment();
    for (var i = 0; i < count; i++) {
      var top = rand(i, 1, seed) * 100;
      var left = rand(i, 2, seed) * 100;
      var size = rand(i, 3, seed);
      var amber = rand(i, 4, seed) > 0.86;
      var lg = size > 0.85;
      var el = document.createElement("i");
      var cls = [];
      if (lg) cls.push("lg");
      if (amber) cls.push("amber");
      if (cls.length) el.className = cls.join(" ");
      el.style.top = top + "%";
      el.style.left = left + "%";
      frag.appendChild(el);
    }
    host.appendChild(frag);
  }

  function wireCopyButtons(root) {
    var buttons = root.querySelectorAll("[data-ml-install]");
    buttons.forEach(function (btn) {
      var copy = btn.querySelector(".ml-copy");
      var cmdEl = btn.querySelector(".ml-cmd");
      if (!copy || !cmdEl) return;
      copy.addEventListener("click", function () {
        var text = cmdEl.textContent || "";
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text);
          }
        } catch (e) {}
        var prev = copy.textContent;
        copy.textContent = "COPIED";
        setTimeout(function () { copy.textContent = prev; }, 1400);
      });
    });
  }

  function init() {
    document.querySelectorAll(".ml-stars[data-count]").forEach(renderStars);
    wireCopyButtons(document);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
