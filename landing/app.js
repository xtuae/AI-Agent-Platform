// heyozo.com: progressive enhancement only. Without this file every page is complete and readable.
(function () {
  "use strict";

  // Where "Book a demo" goes. Empty: the buttons scroll to the closing section instead.
  var DEMO_URL = "https://calendly.com/hello-hmhlabz/heyozo-20-mins-call-demo";

  // Sub-domains that are not client dashboards.
  var RESERVED = ["api", "go", "status", "www", "admin"];

  var root = document.documentElement;
  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  // Set before first paint (this script is not deferred) so hidden-until-animated content never flashes.
  root.classList.add("js");

  var AR = {
    skip: "انتقل إلى المحتوى",
    by: "من HMH Labz",
    login: "دخول العملاء",
    demo: "احجز عرضًا تجريبيًا",
    h1: "نشاطك التجاري يرد على واتساب وتيليجرام. ليلًا ونهارًا.",
    lede: "يرد Heyozo على عملائك بالعربية والإنجليزية، ويستقبل الطلبات والحجوزات من كتالوجك أنت، ويحوّل المحادثة إلى فريقك فور الحاجة إلى شخص.",
    how_link: "كيف يعمل",
    fact1: "منصة واتساب للأعمال الرسمية",
    fact2: "رقمك يبقى ملكك",
    fact3: "بالعربية والإنجليزية منذ اليوم الأول",
    fact4: "وتيليجرام أيضًا، عبر حسابك الآلي الخاص",
    replay: "إعادة",
    show_title: "يقوم بعمل مكتب الاستقبال لديك على واتساب وتيليجرام",
    r1_t: "يستقبل الطلبات من كتالوجك الحقيقي",
    r1_d: "الأسعار والمخزون والكوبونات من كتالوجك، دون أي اختلاق. ويسأل العملاء عن طلباتهم في أي وقت فيحصلون على حالتها الفعلية.",
    r2_t: "يحجز المواعيد دون تعارض",
    r2_d: "يحجز العملاء مواعيدهم أو يغيّرونها أو يلغونها، ولا يعرض Heyozo إلا الأوقات المتاحة في تقويمك، فلا يتكرر حجز الموعد نفسه.",
    r3_t: "يعرف متى يجب أن يرد شخص",
    r3_d: "الشكاوى والمبالغ المستردة وطلبات الخصم والطلبات الكبيرة تذهب مباشرة إلى فريقك مع المحادثة كاملة، ويُبلَّغ العميل بأن أحدًا يتابع طلبه.",
    a1_t: "العربية والإنجليزية والعربيزي",
    a1_d: "يرد بأسلوب العميل نفسه، ويفهم الرسائل الصوتية.",
    a2_t: "حملات وافق عليها العملاء",
    a2_d: "يشترك العملاء بمسح رمز QR، على واتساب أو تيليجرام، وتصل العروض فقط لمن وافق.",
    a3_t: "لوحة تحكم مباشرة خاصة بك",
    a3_d: "كل المحادثات والطلبات والعملاء، على yourname.heyozo.com.",
    how_title: "كيف تبدأ",
    s1_t: "عرّفنا على نشاطك",
    s1_d: "ما تبيعه أو تحجزه، وأسعارك، والأسلوب الذي تحب أن تخاطب به عملاءك.",
    s2_t: "نربط رقم واتساب وحساب تيليجرام الآلي الخاصين بك",
    s2_d: "عبر منصة واتساب للأعمال الرسمية وواجهة Bot API من تيليجرام، ويبقى الرقم والحساب ملكك.",
    s3_t: "يبدأ Heyozo بالرد",
    s3_d: "ترى كل محادثة في لوحة التحكم ويمكنك التدخل متى شئت.",
    c_title: "شاهده يرد على عملائك أنت",
    c_d: "عرض تجريبي مدته ٢٠ دقيقة، بمنتجاتك داخل المحادثة.",
    foot_a: "Heyozo منتج من",
    nav_privacy: "الخصوصية",
    nav_terms: "الشروط",
    nav_dpa: "معالجة البيانات",
    nav_aup: "الاستخدام المقبول",
    l_title: "انتقل إلى لوحة التحكم",
    l_label: "اسم لوحة التحكم الخاصة بك",
    l_err: "استخدم الحروف الإنجليزية والأرقام والشرطات فقط.",
    cancel: "إلغاء",
    go: "متابعة",
    chat_in: "مرحبا، أبي ٥ كراتين ماي ٥٠٠ مل لبكرة"
  };
  // Keys whose element must also switch writing direction (the phone mock-up itself stays ltr).
  var DIRECTIONAL = ["chat_in"];

  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  document.addEventListener("DOMContentLoaded", function () {
    // ---------- header border once the page scrolls
    var top = document.getElementById("top");
    if (top) {
      var onScroll = function () { top.classList.toggle("scrolled", window.scrollY > 8); };
      window.addEventListener("scroll", onScroll, { passive: true });
      onScroll();
    }

    // ---------- language (landing page only: the legal pages are English)
    var langBtn = document.getElementById("lang");
    var nodes = document.querySelectorAll("[data-i18n]");
    var EN = {};
    nodes.forEach(function (n) { EN[n.getAttribute("data-i18n")] = n.textContent; });
    var titles = { en: document.title, ar: "Heyozo · وكلاء ذكاء اصطناعي لواتساب وتيليجرام" };
    function setLang(lang) {
      var dict = lang === "ar" ? AR : EN;
      nodes.forEach(function (n) {
        var k = n.getAttribute("data-i18n");
        if (dict[k]) n.textContent = dict[k];
        if (DIRECTIONAL.indexOf(k) >= 0) {
          n.dir = lang === "ar" ? "rtl" : "ltr";
          n.lang = lang;
        }
      });
      root.lang = lang;
      root.dir = lang === "ar" ? "rtl" : "ltr";
      document.title = titles[lang];
      langBtn.textContent = lang === "ar" ? "English" : "العربية";
      langBtn.lang = lang === "ar" ? "en" : "ar";
      store("lang", lang);
    }
    if (langBtn) {
      langBtn.addEventListener("click", function () { setLang(root.lang === "ar" ? "en" : "ar"); });
      var saved = load("lang");
      if (saved === "ar" || (!saved && /^ar\b/i.test(navigator.language || ""))) setLang("ar");
    }

    // ---------- demo links
    if (DEMO_URL) {
      document.querySelectorAll(".demo-link").forEach(function (a) {
        a.href = DEMO_URL;
        a.target = "_blank";
        a.rel = "noopener";
      });
    }

    // ---------- reveal on scroll
    var reveal = document.querySelectorAll("[data-reveal]");
    if (calm || !("IntersectionObserver" in window)) {
      reveal.forEach(function (el) { el.classList.add("in"); });
    } else {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
        });
      }, { rootMargin: "0px 0px -12% 0px", threshold: 0.12 });
      reveal.forEach(function (el) { io.observe(el); });
    }

    // ---------- the hero conversation plays out like a real chat
    var chat = document.getElementById("hero-chat");
    var replay = document.getElementById("replay");
    var presence = document.getElementById("presence");
    if (chat && !calm) {
      var msgs = Array.prototype.slice.call(chat.querySelectorAll(".msg"));
      msgs.forEach(function (m) { m.classList.add("pending"); });
      var run = 0;
      var play = async function () {
        var my = ++run;
        replay.hidden = true;
        msgs.forEach(function (m) { m.classList.add("pending"); m.classList.remove("arrive"); });
        await wait(500);
        for (var i = 0; i < msgs.length; i++) {
          if (my !== run) return;
          var m = msgs[i];
          var side = m.classList.contains("out") ? "out" : m.classList.contains("in") ? "in" : null;
          if (side) {
            var dots = document.createElement("span");
            dots.className = "dots " + side;
            dots.setAttribute("aria-hidden", "true");
            dots.innerHTML = "<i></i><i></i><i></i>";
            chat.insertBefore(dots, m);
            if (side === "out") { presence.textContent = "typing…"; presence.classList.add("typing"); }
            await wait(side === "out" ? Math.min(650 + m.textContent.length * 9, 1700) : 750);
            dots.remove();
            presence.textContent = "online";
            presence.classList.remove("typing");
          } else {
            await wait(450);
          }
          if (my !== run) return;
          m.classList.remove("pending");
          m.classList.add("arrive");
          await wait(side === "in" ? 500 : 650);
        }
        replay.hidden = false;
      };
      var started = false;
      var start = function () { if (!started) { started = true; play(); } };
      if ("IntersectionObserver" in window) {
        var cio = new IntersectionObserver(function (es) {
          if (es[0].isIntersecting) { start(); cio.disconnect(); }
        }, { threshold: 0.3 });
        cio.observe(chat);
      } else {
        start();
      }
      replay.addEventListener("click", play);
    }

    // ---------- client login: <slug>.heyozo.com
    var dlg = document.getElementById("login");
    if (dlg) {
      var input = document.getElementById("slug");
      var err = document.getElementById("slug-err");
      var host = location.hostname.replace(/^www\./, "");
      var opener = document.getElementById("login-open");
      opener.addEventListener("click", function () {
        err.hidden = true;
        input.removeAttribute("aria-invalid");
        if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
        input.focus();
      });
      document.getElementById("login-cancel").addEventListener("click", function () { dlg.close(); });
      dlg.addEventListener("close", function () { opener.focus(); });
      input.addEventListener("input", function () { err.hidden = true; input.removeAttribute("aria-invalid"); });
      document.getElementById("login-form").addEventListener("submit", function (e) {
        e.preventDefault();
        var slug = input.value.trim().toLowerCase();
        if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(slug) || RESERVED.indexOf(slug) !== -1) {
          err.hidden = false;
          input.setAttribute("aria-invalid", "true");
          input.focus();
          return;
        }
        location.href = "https://" + slug + "." + host + "/";
      });
    }

    document.querySelectorAll(".year").forEach(function (y) { y.textContent = String(new Date().getFullYear()); });
  });
})();
