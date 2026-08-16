"use strict";
/* Chart primitives for the pi-streamer GUI.
 *
 * Deliberately dependency-free inline SVG: this page is served off an SD card
 * to a browser on an event network that may have no route to the internet, so
 * a CDN chart library is not an option.
 *
 * Every chart here is single-series by design. Two measures of different scale
 * get two charts (small multiples), never two y-axes on one plot.
 */

const VIZ = (() => {
  const NS = "http://www.w3.org/2000/svg";

  const el = (name, attrs) => {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v !== null && v !== undefined) node.setAttribute(k, String(v));
    }
    return node;
  };

  const clean = (values) => values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));

  function niceMax(values, floor) {
    const vals = clean(values);
    const raw = Math.max(floor || 1, ...(vals.length ? vals : [floor || 1]));
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
    return step * mag;
  }

  /* 12-point sparkline: trend line in the de-emphasis hue, current value in
     the accent, per the stat-tile contract. */
  function sparkline(values, accent, opts) {
    const o = Object.assign({ w: 104, h: 26, points: 12 }, opts || {});
    const svg = el("svg", {
      width: o.w, height: o.h, viewBox: `0 0 ${o.w} ${o.h}`,
      class: "spark", "aria-hidden": "true", focusable: "false",
    });
    const series = values.slice(-o.points);
    const vals = clean(series);
    if (vals.length < 2) return svg;

    const max = Math.max(...vals);
    const min = Math.min(...vals);
    const span = max - min || 1;
    const pad = 3;
    const x = (i) => (i / (series.length - 1)) * (o.w - 2 * pad) + pad;
    const y = (v) => o.h - pad - ((v - min) / span) * (o.h - 2 * pad);

    let d = "";
    let lastPoint = null;
    series.forEach((v, i) => {
      if (v === null || v === undefined || Number.isNaN(v)) return;
      const px = x(i), py = y(v);
      d += (d ? " L" : "M") + `${px.toFixed(1)} ${py.toFixed(1)}`;
      lastPoint = [px, py];
    });
    if (!d) return svg;

    svg.appendChild(el("path", {
      d, fill: "none", stroke: "var(--spark-muted)",
      "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round",
    }));
    if (lastPoint) {
      // 2px surface ring so the accent dot stays legible over the line.
      svg.appendChild(el("circle", {
        cx: lastPoint[0], cy: lastPoint[1], r: 4.5, fill: "var(--panel-2)",
      }));
      svg.appendChild(el("circle", {
        cx: lastPoint[0], cy: lastPoint[1], r: 2.75, fill: accent,
      }));
    }
    return svg;
  }

  /* Single-series area chart over time, with crosshair + tooltip on hover. */
  function areaChart(mount, cfg) {
    const values = cfg.values || [];
    const times = cfg.times || [];
    const accent = cfg.color;
    const unit = cfg.unit || "";
    const W = cfg.width || 640, H = cfg.height || 132;
    const padL = 42, padR = 10, padT = 12, padB = 20;

    mount.innerHTML = "";
    const vals = clean(values);
    if (vals.length < 2) {
      const empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = "Collecting data…";
      mount.appendChild(empty);
      return;
    }

    const max = cfg.max !== undefined ? cfg.max : niceMax(values, cfg.floor);
    const svg = el("svg", {
      viewBox: `0 0 ${W} ${H}`, class: "chart", preserveAspectRatio: "none",
      role: "img", "aria-label": `${cfg.label} over the last ${cfg.windowLabel || "10 minutes"}`,
    });

    const x = (i) => padL + (i / Math.max(1, values.length - 1)) * (W - padL - padR);
    const y = (v) => padT + (1 - Math.min(v, max) / max) * (H - padT - padB);

    // Recessive gridlines and ticks.
    [0, 0.5, 1].forEach((frac) => {
      const gy = padT + frac * (H - padT - padB);
      svg.appendChild(el("line", {
        x1: padL, x2: W - padR, y1: gy, y2: gy,
        stroke: "var(--line)", "stroke-width": 1,
      }));
      const label = el("text", {
        x: padL - 6, y: gy + 3.5, "text-anchor": "end", class: "tick",
      });
      label.textContent = fmtTick(max * (1 - frac));
      svg.appendChild(label);
    });

    let line = "", area = "";
    let started = false;
    values.forEach((v, i) => {
      if (v === null || v === undefined || Number.isNaN(v)) return;
      const px = x(i), py = y(v);
      if (!started) { line = `M${px} ${py}`; area = `M${px} ${H - padB} L${px} ${py}`; started = true; }
      else { line += ` L${px} ${py}`; area += ` L${px} ${py}`; }
    });
    const lastIdx = values.length - 1;
    area += ` L${x(lastIdx)} ${H - padB} Z`;

    const gradId = `g-${cfg.id}`;
    const defs = el("defs", {});
    const grad = el("linearGradient", { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(el("stop", { offset: "0%", "stop-color": accent, "stop-opacity": 0.28 }));
    grad.appendChild(el("stop", { offset: "100%", "stop-color": accent, "stop-opacity": 0.02 }));
    defs.appendChild(grad);
    svg.appendChild(defs);

    svg.appendChild(el("path", { d: area, fill: `url(#${gradId})`, stroke: "none" }));
    svg.appendChild(el("path", {
      d: line, fill: "none", stroke: accent, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round",
      "vector-effect": "non-scaling-stroke",
    }));

    // Hover layer: crosshair + marker, tooltip in HTML above the SVG.
    const crosshair = el("line", {
      y1: padT, y2: H - padB, stroke: "var(--muted)", "stroke-width": 1,
      "stroke-dasharray": "3 3", opacity: 0,
    });
    const marker = el("circle", { r: 4.5, fill: accent, stroke: "var(--panel)", "stroke-width": 2, opacity: 0 });
    svg.appendChild(crosshair);
    svg.appendChild(marker);

    mount.appendChild(svg);
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    mount.appendChild(tip);

    const onMove = (evt) => {
      const rect = svg.getBoundingClientRect();
      const relX = ((evt.clientX - rect.left) / rect.width) * W;
      let idx = Math.round(((relX - padL) / (W - padL - padR)) * (values.length - 1));
      idx = Math.max(0, Math.min(values.length - 1, idx));
      const v = values[idx];
      if (v === null || v === undefined) { onLeave(); return; }
      crosshair.setAttribute("x1", x(idx)); crosshair.setAttribute("x2", x(idx));
      crosshair.setAttribute("opacity", 1);
      marker.setAttribute("cx", x(idx)); marker.setAttribute("cy", y(v));
      marker.setAttribute("opacity", 1);
      const when = times[idx] ? relTime(times[idx]) : "";
      tip.innerHTML = `<strong>${fmtVal(v)}${unit}</strong><span>${when}</span>`;
      tip.style.opacity = 1;
      const px = (x(idx) / W) * rect.width;
      tip.style.left = Math.max(4, Math.min(rect.width - 4, px)) + "px";
    };
    const onLeave = () => {
      crosshair.setAttribute("opacity", 0);
      marker.setAttribute("opacity", 0);
      tip.style.opacity = 0;
    };
    // Generous hit target: the whole plot area, not just the 2px line.
    svg.addEventListener("mousemove", onMove);
    svg.addEventListener("mouseleave", onLeave);
  }

  function fmtTick(v) {
    if (v >= 1000) return (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k";
    if (v >= 10) return v.toFixed(0);
    return v.toFixed(1);
  }
  function fmtVal(v) {
    if (typeof v !== "number") return "—";
    if (Math.abs(v) >= 100) return v.toFixed(0);
    if (Math.abs(v) >= 10) return v.toFixed(1);
    return v.toFixed(2);
  }
  function relTime(ts) {
    const secs = Math.round(Date.now() / 1000 - ts);
    if (secs < 5) return "just now";
    if (secs < 60) return `${secs}s ago`;
    const mins = Math.floor(secs / 60);
    return `${mins}m ${secs % 60}s ago`;
  }

  return { sparkline, areaChart, fmtVal, relTime, niceMax };
})();
