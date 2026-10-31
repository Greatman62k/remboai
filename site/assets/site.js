// RemboAI site: mobile nav, scroll reveals, chat playback. No framework, no tracking.
(function () {
  var toggle = document.querySelector(".nav-toggle");
  var nav = document.getElementById("main-nav");
  if (toggle && nav) {
    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    nav.addEventListener("click", function (e) {
      if (e.target.tagName === "A") {
        nav.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });
  }
  var year = document.getElementById("year");
  if (year) year.textContent = new Date().getFullYear();

  if ("IntersectionObserver" in window) {
    var show = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add("visible");
          show.unobserve(e.target);
        }
      });
    }, { threshold: 0.15 });
    document.querySelectorAll(".reveal, .feature-row").forEach(function (el) {
      show.observe(el);
    });
    var chat = document.querySelector(".chat-mock");
    if (chat) {
      var play = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) {
            chat.classList.add("playing");
            play.disconnect();
          }
        });
      }, { threshold: 0.4 });
      play.observe(chat);
    }
  } else {
    document.querySelectorAll(".reveal, .feature-row").forEach(function (el) {
      el.classList.add("visible");
    });
    var fallback = document.querySelector(".chat-mock");
    if (fallback) fallback.classList.add("playing");
  }
})();
