// heyozo.com landing page: language toggle (English / Arabic), demo link and client login.
(function () {
  "use strict";

  // Where "Book a demo" goes. Set this to the booking form or calendar link.
  var DEMO_URL = "";

  // Sub-domains that are not client dashboards.
  var RESERVED = ["api", "go", "status", "www", "admin"];

  var AR = {
    by: "من HMH Labz",
    login: "دخول العملاء",
    demo: "احجز عرضًا تجريبيًا",
    eyebrow: "وكلاء ذكاء اصطناعي لواتساب للأعمال",
    h1: "نشاطك التجاري يرد على واتساب. ليلًا ونهارًا.",
    lede: "يرد Heyozo على عملائك بالعربية والإنجليزية، ويستقبل الطلبات والحجوزات من كتالوجك الحقيقي، ويحوّل المحادثة إلى فريقك فور الحاجة إلى شخص.",
    how_link: "كيف يعمل",
    fine: "صُنع في الإمارات. يعمل على منصة واتساب للأعمال الرسمية.",
    online: "متصل",
    note: "طلب خصم؟ تم تحويله إلى الفريق.",
    f_title: "كل ما يفعله مكتب الاستقبال لديك على واتساب",
    f1_t: "يتحدث بلغة عملائك",
    f1_d: "العربية والإنجليزية والعربيزي، ويرد بنفس الأسلوب. ويفهم الرسائل الصوتية أيضًا.",
    f2_t: "طلبات من كتالوجك الحقيقي",
    f2_d: "الأسعار والمخزون والكوبونات من كتالوجك، دون أي اختلاق. ويتابع العملاء حالة طلباتهم في أي وقت.",
    f3_t: "حجوزات بلا تعارض",
    f3_d: "يحجز العملاء مواعيدهم أو يغيّرونها أو يلغونها. ولا تُعرض إلا الأوقات المتاحة فعلًا.",
    f4_t: "يعرف متى يستدعي موظفًا",
    f4_d: "الشكاوى والمبالغ المستردة والخصومات والطلبات الكبيرة تذهب مباشرة إلى فريقك مع المحادثة كاملة.",
    f5_t: "حملات وافق عليها العملاء",
    f5_d: "يشترك العملاء عبر رمز QR، فترسل العروض والتذكيرات فقط لمن وافق.",
    f6_t: "لوحة تحكم مباشرة خاصة بك",
    f6_d: "كل المحادثات والطلبات والعملاء في مكان واحد، على yourname.heyozo.com.",
    how_title: "جاهز خلال أيام، لا أشهر",
    s1_t: "عرّفنا على نشاطك",
    s1_d: "منتجاتك أو خدماتك، وأسعارك، والأسلوب الذي تحب أن تخاطب به عملاءك.",
    s2_t: "نربط رقم واتساب الخاص بك",
    s2_d: "على منصة واتساب للأعمال الرسمية. ويبقى رقمك ملكك.",
    s3_t: "يبدأ Heyozo بالرد",
    s3_d: "تتابع كل محادثة من لوحة التحكم وتتدخل متى شئت.",
    c_title: "شاهده وهو يرد على عملائك",
    c_d: "عرض تجريبي مدته ٢٠ دقيقة بمنتجاتك أنت داخل المحادثة.",
    foot: "Heyozo منتج من HMH Labz، دبي.",
    l_title: "انتقل إلى لوحة التحكم",
    l_label: "اسم لوحة التحكم الخاصة بك",
    l_err: "استخدم الحروف الإنجليزية والأرقام والشرطات فقط.",
    cancel: "إلغاء",
    go: "متابعة"
  };

  var nodes = document.querySelectorAll("[data-i18n]");
  var EN = {};
  nodes.forEach(function (n) { EN[n.getAttribute("data-i18n")] = n.textContent; });
  var titles = { en: document.title, ar: "Heyozo · وكلاء ذكاء اصطناعي لواتساب" };

  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }

  var langBtn = document.getElementById("lang");
  function setLang(lang) {
    var dict = lang === "ar" ? AR : EN;
    nodes.forEach(function (n) {
      var k = n.getAttribute("data-i18n");
      if (dict[k]) n.textContent = dict[k];
    });
    document.documentElement.lang = lang;
    document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
    document.title = titles[lang];
    langBtn.textContent = lang === "ar" ? "English" : "العربية";
    store("lang", lang);
  }
  langBtn.addEventListener("click", function () {
    setLang(document.documentElement.lang === "ar" ? "en" : "ar");
  });
  var saved = load("lang");
  if (saved === "ar" || (!saved && /^ar\b/i.test(navigator.language || ""))) setLang("ar");

  // Demo links
  document.querySelectorAll(".demo-link").forEach(function (a) {
    if (DEMO_URL) {
      a.href = DEMO_URL;
      a.target = "_blank";
      a.rel = "noopener";
    }
  });

  // Client login: <slug>.heyozo.com
  var dlg = document.getElementById("login");
  var input = document.getElementById("slug");
  var err = document.getElementById("slug-err");
  var host = location.hostname.replace(/^www\./, "");
  document.getElementById("login-open").addEventListener("click", function () {
    err.hidden = true;
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
    input.focus();
  });
  document.getElementById("login-cancel").addEventListener("click", function () { dlg.close(); });
  document.getElementById("login-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var slug = input.value.trim().toLowerCase();
    if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(slug) || RESERVED.indexOf(slug) !== -1) {
      err.hidden = false;
      return;
    }
    location.href = "https://" + slug + "." + host + "/";
  });

  document.getElementById("year").textContent = String(new Date().getFullYear());
})();
