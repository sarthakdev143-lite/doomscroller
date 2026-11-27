// The pricing moment (redesign/c.html #pricing). Runs in every mode: nothing here needs WebGL or motion.
//   - the strip: dots that share a price are dodged into rows for the real width; hover or focus shows the plan;
//     labels that would collide are dropped (the table under "All plans" has every row);
//   - the plans: a switch shows yearly prices (the markup carries both, so it works without this script as well).
import { track } from "../site.js";

const ROW_GAP = 15;

function dodge(svg) {
  const width = svg.getBoundingClientRect().width;
  const height = svg.getBoundingClientRect().height;
  if (!width || !height) return;
  const mid = height * 0.5;
  const dots = Array.from(svg.querySelectorAll(".rc-dot"));
  const placed = new Map(); // row -> [x px]
  const order = [0, -1, 1, -2, 2, -3, 3];
  dots
    .map((dot) => ({ dot, x: (parseFloat(dot.getAttribute("cx")) / 100) * width, r: Number(dot.getAttribute("r")) }))
    .sort((a, b) => a.x - b.x)
    .forEach(({ dot, x, r }) => {
      const gap = r * 2 + 2;
      const row = order.find((k) => (placed.get(k) || []).every((px) => Math.abs(px - x) >= gap)) ?? 0;
      placed.set(row, [...(placed.get(row) || []), x]);
      dot.setAttribute("cy", String(Math.round(mid + row * ROW_GAP - 6)));
    });

  // Labels: keep the first, drop any that would overlap the one before.
  let right = -Infinity;
  svg.querySelectorAll(".rc-strip__labels text").forEach((label) => {
    label.removeAttribute("visibility");
    const box = label.getBBox();
    if (box.x < right + 12 || box.x + box.width > width + 2) {
      label.setAttribute("visibility", "hidden");
      return;
    }
    right = box.x + box.width;
  });
}

function tooltips(plot) {
  const tip = plot.querySelector("[data-rc-tip]");
  const svg = plot.querySelector("svg");
  if (!tip || !svg) return;
  let hot = null;
  const show = (dot) => {
    const [line, value] = (dot.getAttribute("data-tip") || "").split("|");
    tip.textContent = "";
    const name = document.createElement("span");
    name.textContent = line;
    const figure = document.createElement("strong");
    figure.textContent = value || "";
    tip.append(figure, name);
    const box = svg.getBoundingClientRect();
    const host = plot.getBoundingClientRect();
    const x = (parseFloat(dot.getAttribute("cx")) / 100) * box.width + box.left - host.left;
    const y = Number(dot.getAttribute("cy")) + box.top - host.top;
    tip.hidden = false;
    const half = tip.offsetWidth / 2;
    tip.style.left = `${Math.min(Math.max(x, half), host.width - half)}px`;
    tip.style.top = `${y}px`;
    hot?.classList.remove("is-hot");
    hot = dot;
    dot.classList.add("is-hot");
  };
  const hide = () => {
    tip.hidden = true;
    hot?.classList.remove("is-hot");
    hot = null;
  };
  // One listener for the whole plot: the nearest dot within reach, so a 13px dot has a much larger target.
  svg.addEventListener("pointermove", (e) => {
    let best = null;
    let bestD = 22;
    const box = svg.getBoundingClientRect();
    svg.querySelectorAll(".rc-dot").forEach((dot) => {
      const dx = (parseFloat(dot.getAttribute("cx")) / 100) * box.width + box.left - e.clientX;
      const dy = Number(dot.getAttribute("cy")) + box.top - e.clientY;
      const d = Math.hypot(dx, dy);
      if (d < bestD) {
        bestD = d;
        best = dot;
      }
    });
    if (best && best !== hot) show(best);
    else if (!best) hide();
  });
  svg.addEventListener("pointerleave", hide);
}

function entrance(figure) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !("IntersectionObserver" in window)) return;
  const dots = Array.from(figure.querySelectorAll(".rc-dot"));
  const band = figure.querySelector(".rc-strip__band");
  // Everyone else's dots arrive from the right, ours drop in last: the gap is the point.
  dots.forEach((dot, i) => {
    const ours = dot.classList.contains("rc-dot--us");
    dot.style.transition = "none";
    dot.style.opacity = "0";
    dot.style.transform = ours ? "translateY(-28px)" : "translateX(60px)";
    dot.style.transformBox = "fill-box";
    dot.dataset.delay = String(ours ? 900 + i * 45 : (i - 9) * 22);
  });
  if (band) {
    band.style.transformBox = "fill-box";
    band.style.transformOrigin = "left center";
    band.style.transform = "scaleX(0)";
  }
  const io = new IntersectionObserver((entries) => {
    if (!entries.some((e) => e.isIntersecting)) return;
    io.disconnect();
    dots.forEach((dot) => {
      const ours = dot.classList.contains("rc-dot--us");
      dot.style.transition = `opacity 500ms ease ${dot.dataset.delay}ms, transform ${ours ? 900 : 1100}ms ${ours ? "cubic-bezier(0.34, 1.56, 0.64, 1)" : "cubic-bezier(0.16, 1, 0.3, 1)"} ${dot.dataset.delay}ms`;
      dot.style.opacity = "1";
      dot.style.transform = "none";
    });
    if (band) {
      band.style.transition = "transform 900ms cubic-bezier(0.16, 1, 0.3, 1) 800ms";
      band.style.transform = "none";
    }
  }, { threshold: 0.45 });
  io.observe(figure);
}

function billing(col) {
  const toggle = col.querySelector("[data-rc-yearly]");
  if (!toggle) return;
  toggle.hidden = false;
  // Without this script both prices are in the markup and the monthly ones show, with the yearly ones in the note.
  toggle.addEventListener("click", () => {
    const yearly = toggle.getAttribute("aria-checked") !== "true";
    toggle.setAttribute("aria-checked", String(yearly));
    col.dataset.rcBilling = yearly ? "year" : "month";
    col.querySelectorAll("[data-rc-month]").forEach((el) => (el.hidden = yearly));
    col.querySelectorAll("[data-rc-year]").forEach((el) => (el.hidden = !yearly));
    track("pricing_yearly", { yearly });
  });
}

export function initPricing(root) {
  const figure = root.querySelector("[data-rc-strip]");
  if (figure) {
    const plot = figure.querySelector("[data-rc-plot]");
    const svg = figure.querySelector("svg");
    const redraw = () => dodge(svg);
    // The section is content-visibility: auto. Measuring it now would lay it out, and fetch the face its rupee
    // signs need, on every visit; so the chart is measured only once it comes near.
    const arm = () => {
      redraw();
      if ("ResizeObserver" in window) new ResizeObserver(redraw).observe(svg);
      document.fonts?.ready.then(redraw);
    };
    if ("IntersectionObserver" in window) {
      const near = new IntersectionObserver((entries) => {
        if (!entries.some((e) => e.isIntersecting)) return;
        near.disconnect();
        arm();
      }, { rootMargin: "900px 0px" });
      near.observe(figure);
    } else {
      arm();
    }
    tooltips(plot);
    entrance(figure);
  }
  root.querySelectorAll("[data-rc-billing]").forEach(billing);
}
