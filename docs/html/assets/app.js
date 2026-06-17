/* 極小 scrollspy:高亮側欄目前章節。頁面無此檔仍完整可用。零外部依賴。 */
(function () {
  "use strict";
  var links = Array.prototype.slice.call(document.querySelectorAll(".sidebar a[href^='#']"));
  if (!links.length) return;
  var map = {};
  links.forEach(function (a) {
    var id = a.getAttribute("href").slice(1);
    var el = document.getElementById(id);
    if (el) map[id] = a;
  });
  var ids = Object.keys(map);
  function onScroll() {
    var top = window.scrollY + 80;
    var current = ids[0];
    for (var i = 0; i < ids.length; i++) {
      var el = document.getElementById(ids[i]);
      if (el && el.offsetTop <= top) current = ids[i];
    }
    links.forEach(function (a) { a.classList.remove("active"); });
    if (map[current]) map[current].classList.add("active");
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("load", onScroll);
  onScroll();
})();
