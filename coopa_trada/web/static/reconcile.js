"use strict";
// Reconcile: run it, render the plain-language lines.

const out = document.getElementById("reconcile-out");
const btn = document.getElementById("btn-reconcile");

btn.addEventListener("click", async () => {
  btn.disabled = true;
  out.textContent = "running…";
  const { ok, body } = await CT.post("/api/reconcile", {});
  btn.disabled = false;
  out.innerHTML = "";
  if (!ok) {
    out.append(CT.el("p", { class: "error", text: body.error || "reconcile failed" }));
    return;
  }
  const head = CT.el("p", { class: "hint", text: "mode: " + body.mode + (body.simulated ? " (simulated)" : "") });
  out.append(head);
  const ul = CT.el("ul", { class: "recon-lines" });
  (body.lines || []).forEach((line) => ul.append(CT.el("li", { text: line })));
  out.append(ul);
  if (body.divergences && body.divergences.length) {
    out.classList.add("has-divergence");
  } else {
    out.classList.remove("has-divergence");
  }
});
