"use strict";
const $ = (id) => document.getElementById(id);
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
let me = null, tab = "bookings", day = null;

class AuthError extends Error {}

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "onclick" || k === "onchange" || k === "onsubmit") n[k] = v;
    else if (k === "class") n.className = v;
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) n.append(kid instanceof Node ? kid : document.createTextNode(kid ?? ""));
  return n;
}

function toast(msg, bad) {
  const t = $("toast");
  t.textContent = msg; t.className = "show" + (bad ? " err" : "");
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.className = ""), 3500);
}

async function api(method, path, body) {
  const r = await fetch("/api/owner" + path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Rembo": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch (_) {}
  if (r.status === 401) throw new AuthError((data && data.detail) || "Not signed in");
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => (x.loc || []).slice(1).join(".") + " " + x.msg).join("; ") : d || "Something went wrong");
  }
  return data;
}

const guard = (fn) => async (...a) => {
  try { await fn(...a); } catch (e) { if (e instanceof AuthError) return signedOut(e.message); toast(e.message, true); }
};

function signedOut(msg) {
  $("top").hidden = $("tabs").hidden = true;
  $("main").replaceChildren(el("div", { class: "center" },
    el("h2", {}, "Sign in"),
    el("p", { class: "muted" }, msg || "Open your salon page from WhatsApp."),
    el("p", {}, "Send the word ", el("strong", {}, "link"), " to your salon's WhatsApp number and tap the link you get back.")));
}

function loginPage(token) {
  $("main").replaceChildren(el("div", { class: "center" },
    el("h2", {}, "Open your salon page"),
    el("p", { class: "muted" }, "This link works once."),
    el("button", { onclick: guard(async () => {
      await api("POST", "/session", { token });
      history.replaceState(null, "", "/owner");
      await start();
    }) }, "Open")));
}

const money = (n) => "KES " + Number(n).toLocaleString("en-KE");
const iso = (d) => d.toISOString().slice(0, 10);

async function start() {
  me = await api("GET", "/me");
  $("salon-name").textContent = me.name;
  $("top").hidden = $("tabs").hidden = false;
  const tabs = [["bookings", "Bookings"], ["services", "Services"], ["closed", "Closed"], ["settings", "Settings"]];
  $("tabs").replaceChildren(...tabs.map(([id, label]) => el("button", { role: "tab", "aria-selected": id === tab, onclick: () => { tab = id; start().catch(() => {}); } }, label)));
  const today = new Date().toLocaleDateString("en-CA", { timeZone: me.timezone });
  day = day || today;
  $("today-line").textContent = new Date().toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: me.timezone });
  await ({ bookings: viewBookings, services: viewServices, closed: viewClosed, settings: viewSettings })[tab]();
}

const shiftDay = (d, n) => { const x = new Date(d + "T12:00:00Z"); x.setUTCDate(x.getUTCDate() + n); return iso(x); };

async function viewBookings() {
  const [sum, list, refunds] = await Promise.all([api("GET", "/summary"), api("GET", "/bookings?day=" + day), api("GET", "/refunds")]);
  const rows = list.bookings.map((b) => el("div", { class: "booking" },
    el("div", { class: "time" }, b.time),
    el("div", {}, el("div", {}, b.service + " · " + b.stylist), el("div", { class: "muted" }, (b.client || "Customer") + " · ", el("a", { href: "tel:" + b.phone }, b.phone)),
      el("span", { class: "pill " + (b.paid_kes ? "ok" : "wait") }, b.paid_kes ? "Deposit " + money(b.paid_kes) + " paid" : "Waiting for deposit")),
    el("button", { class: "danger", onclick: guard(async () => {
      if (!confirm("Cancel " + b.service + " at " + b.time + "? The customer will be told.")) return;
      const r = await api("POST", "/bookings/" + b.id + "/cancel");
      toast(r.refund_due_kes ? "Cancelled. Refund " + money(r.refund_due_kes) + " to " + r.refund_to : "Cancelled. Customer told.");
      await start();
    }) }, "Cancel")));
  const picker = el("input", { type: "date", value: day, onchange: guard(async (e) => { day = e.target.value || day; await start(); }) });
  $("main").replaceChildren(
    el("div", { class: "stats" },
      el("div", { class: "stat" }, el("b", {}, money(sum.deposits_today_kes)), el("span", { class: "muted" }, sum.deposits_today_count + " deposit(s) today")),
      el("div", { class: "stat" }, el("b", {}, String(list.bookings.length)), el("span", { class: "muted" }, "bookings that day"))),
    refunds.length ? el("div", { class: "banner" }, el("strong", {}, "Refunds to send from your till"),
      ...refunds.map((r) => el("div", { class: "row" }, el("span", {}, money(r.amount_kes) + " to " + r.phone + (r.receipt ? " (" + r.receipt + ")" : "") + " · " + (r.reason || "")),
        el("button", { class: "ghost", onclick: guard(async () => { await api("POST", "/refunds/" + r.id + "/done"); await start(); }) }, "Done")))) : "",
    el("div", { class: "daynav" },
      el("button", { class: "ghost", onclick: guard(async () => { day = shiftDay(day, -1); await start(); }) }, "‹"), picker,
      el("button", { class: "ghost", onclick: guard(async () => { day = shiftDay(day, 1); await start(); }) }, "›")),
    el("div", { class: "card" }, rows.length ? rows : el("div", { class: "muted" }, "Nobody booked this day.")));
}

async function viewServices() {
  const list = await api("GET", "/services");
  const row = (s) => {
    const price = el("input", { type: "number", min: 0, value: s.price_kes, "aria-label": "Price" });
    const mins = el("input", { type: "number", min: 5, step: 5, value: s.duration_min, "aria-label": "Minutes" });
    const aliases = el("input", { value: s.aliases.join(", "), placeholder: "other names, comma separated", "aria-label": "Other names" });
    return el("div", { class: "card" },
      el("div", { class: "row" }, el("strong", {}, s.name), el("span", { class: "pill " + (s.active ? "ok" : "") }, s.active ? "Offered" : "Hidden")),
      el("div", { class: "grid2" }, el("label", {}, "Price (KES)", price), el("label", {}, "Minutes", mins)),
      el("label", {}, "Other names customers use", aliases),
      el("div", { class: "row" },
        el("button", { onclick: guard(async () => {
          await api("PATCH", "/services/" + s.id, { price_kes: +price.value, duration_min: +mins.value, aliases: aliases.value.split(",") });
          toast("Saved"); await start();
        }) }, "Save"),
        el("button", { class: "ghost", onclick: guard(async () => { await api("PATCH", "/services/" + s.id, { active: !s.active }); await start(); }) }, s.active ? "Hide" : "Offer again")));
  };
  const name = el("input", { placeholder: "e.g. Fade", required: true });
  const price = el("input", { type: "number", min: 0, placeholder: "500", required: true });
  const mins = el("input", { type: "number", min: 5, step: 5, placeholder: "45", required: true });
  const alias = el("input", { placeholder: "e.g. kinyozi, cut" });
  $("main").replaceChildren(...list.map(row),
    el("form", { class: "card", onsubmit: guard(async (e) => {
      e.preventDefault();
      await api("POST", "/services", { name: name.value, price_kes: +price.value, duration_min: +mins.value, aliases: alias.value.split(",") });
      toast("Service added"); await start();
    }) }, el("h2", {}, "Add a service"), el("label", {}, "Name", name),
      el("div", { class: "grid2" }, el("label", {}, "Price (KES)", price), el("label", {}, "Minutes", mins)),
      el("label", {}, "Other names (optional)", alias), el("button", {}, "Add")));
}

async function viewClosed() {
  const list = await api("GET", "/time_off");
  const when = (x) => new Date(x.starts_at).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: me.timezone }) +
    " → " + new Date(x.ends_at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: me.timezone });
  const date = el("input", { type: "date", required: true, value: shiftDay(day, 0) });
  const from = el("input", { type: "time" }), to = el("input", { type: "time" }), note = el("input", { placeholder: "optional" });
  $("main").replaceChildren(
    el("form", { class: "card", onsubmit: guard(async (e) => {
      e.preventDefault();
      const r = await api("POST", "/time_off", { date: date.value, start: from.value || null, end: to.value || null, note: note.value || null });
      toast(r.existing_bookings_in_that_time.length ? "Closed. " + r.existing_bookings_in_that_time.length + " booking(s) fall in that time: cancel them from Bookings." : "Closed. Customers will not be offered that time.");
      await start();
    }) }, el("h2", {}, "Close some time"), el("label", {}, "Day", date),
      el("div", { class: "grid2" }, el("label", {}, "From (leave empty for the whole day)", from), el("label", {}, "To", to)),
      el("label", {}, "Note", note), el("button", {}, "Close it")),
    el("div", { class: "card" }, el("h2", {}, "Closed times"), list.length ? list.map((x) => el("div", { class: "row" }, el("span", {}, when(x) + (x.note ? " · " + x.note : "")),
      el("button", { class: "ghost", onclick: guard(async () => { await api("DELETE", "/time_off/" + x.id); await start(); }) }, "Reopen"))) : el("div", { class: "muted" }, "Nothing closed.")));
}

async function viewSettings() {
  const staff = await api("GET", "/staff");
  const name = el("input", { value: me.name }), deposit = el("input", { type: "number", min: 0, max: 5000, value: me.deposit_kes });
  const hrs = DAYS.map((d) => {
    const h = me.open_hours[d];
    return { d, on: el("input", { type: "checkbox", checked: !!h, "aria-label": d + " open" }), a: el("input", { type: "time", value: h ? h[0] : "08:00" }), b: el("input", { type: "time", value: h ? h[1] : "19:00" }) };
  });
  const stylist = el("input", { placeholder: "Stylist name" });
  $("main").replaceChildren(
    el("div", { class: "card" }, el("h2", {}, "Salon"), el("label", {}, "Name", name),
      el("label", {}, "Deposit to book (KES, 0 for none)", deposit),
      el("div", { class: "muted" }, me.payment ? "Deposits go to your " + me.payment.kind + " " + me.payment.party_b + "." : "No till set up: bookings are confirmed without a deposit."),
      el("button", { onclick: guard(async () => { await api("PATCH", "/salon", { name: name.value, deposit_kes: +deposit.value }); toast("Saved"); await start(); }) }, "Save")),
    el("div", { class: "card" }, el("h2", {}, "Opening hours"),
      ...hrs.map((r) => el("div", { class: "hours" }, el("strong", {}, r.d.toUpperCase()), r.on, r.a, r.b)),
      el("button", { onclick: guard(async () => {
        const open_hours = {};
        for (const r of hrs) open_hours[r.d] = r.on.checked ? [r.a.value, r.b.value] : null;
        await api("PATCH", "/salon", { open_hours }); toast("Hours saved"); await start();
      }) }, "Save hours")),
    el("div", { class: "card" }, el("h2", {}, "Stylists"),
      ...staff.map((s) => el("div", { class: "row" }, el("span", {}, s.name + (s.active ? "" : " (not taking bookings)")),
        el("button", { class: "ghost", onclick: guard(async () => { await api("PATCH", "/staff/" + s.id, { active: !s.active }); await start(); }) }, s.active ? "Pause" : "Resume"))),
      el("div", { class: "row" }, stylist, el("button", { onclick: guard(async () => { if (!stylist.value.trim()) return; await api("POST", "/staff", { name: stylist.value }); await start(); }) }, "Add"))));
}

$("signout").onclick = guard(async () => { await api("POST", "/signout"); signedOut("You are signed out."); });

(async () => {
  const m = location.pathname.match(/^\/owner\/login\/([\w-]+)$/);
  if (m) return loginPage(m[1]);
  try { await start(); } catch (e) { if (e instanceof AuthError) signedOut(e.message); else toast(e.message, true); }
})();
