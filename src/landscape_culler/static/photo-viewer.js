/* Photo inspection owns only transient view state; ratings remain in app.js. */
(() => {
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  function layout(width, height, naturalWidth, naturalHeight, zoom, center) {
    if (!(width > 0 && height > 0 && naturalWidth > 0 && naturalHeight > 0)) return null;
    const scale = zoom === "fit" ? Math.min(width / naturalWidth, height / naturalHeight) : Number(zoom);
    const w = naturalWidth * scale, h = naturalHeight * scale;
    return {width: w, height: h,
      left: w <= width ? (width - w) / 2 : clamp(width / 2 - center.x * w, width - w, 0),
      top: h <= height ? (height - h) / 2 : clamp(height / 2 - center.y * h, height - h, 0)};
  }

  class PhotoReviewViewer {
    constructor(dialog, {selectPhoto, ratings, score}) {
      this.dialog = dialog;
      this.selectPhoto = selectPhoto;
      this.ratings = ratings;
      this.score = score;
      this.query = selector => dialog.querySelector(selector);
      this.panes = [...dialog.querySelectorAll(".inspection-pane")];
      this.reset();
      this.query("#dialog-compare").addEventListener("click", () => this.toggleCompare());
      this.query("#dialog-pin").addEventListener("click", () => this.pinCurrent());
      for (const button of dialog.querySelectorAll("[data-inspection-zoom]")) {
        button.addEventListener("click", () => this.setZoom(button.dataset.inspectionZoom));
      }
      for (const pane of this.panes) {
        const stage = pane.querySelector(".inspection-stage");
        pane.querySelector("img").addEventListener("load", () => this.renderGeometry());
        stage.addEventListener("dblclick", event => {
          if (event.target.closest("button")) return;
          this.setZoom(this.zoom === "fit" ? "1" : "fit");
        });
        stage.addEventListener("pointerdown", event => {
          if (event.button !== 0 || this.zoom === "fit" || event.target.closest("button")) return;
          const image = pane.querySelector("img");
          const geometry = layout(stage.clientWidth, stage.clientHeight, image.naturalWidth, image.naturalHeight, this.zoom, this.center);
          if (!geometry) return;
          // Start from the rendered (edge-clamped) center to avoid sticky edges.
          this.drag = {id: event.pointerId, x: event.clientX, y: event.clientY, stage, geometry,
            center: {x: (stage.clientWidth / 2 - geometry.left) / geometry.width,
                     y: (stage.clientHeight / 2 - geometry.top) / geometry.height}};
          stage.setPointerCapture(event.pointerId);
          event.preventDefault();
        });
        stage.addEventListener("pointermove", event => {
          if (this.drag?.id !== event.pointerId || this.drag.stage !== stage) return;
          this.center = {
            x: clamp(this.drag.center.x - (event.clientX - this.drag.x) / this.drag.geometry.width, 0, 1),
            y: clamp(this.drag.center.y - (event.clientY - this.drag.y) / this.drag.geometry.height, 0, 1),
          };
          this.renderGeometry();
        });
        for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
          stage.addEventListener(name, () => { this.drag = null; });
        }
      }
      this.resizeObserver = new ResizeObserver(() => this.renderGeometry());
      this.panes.forEach(pane => this.resizeObserver.observe(pane.querySelector(".inspection-stage")));
    }

    reset() {
      this.referenceIndex = null;
      this.current = null;
      this.items = [];
      this.zoom = "fit";
      this.center = {x: .5, y: .5};
      this.drag = null;
      this.dialog.classList.remove("comparing");
    }

    setPhoto(item, items, grouping) {
      if (this.current && this.current.group_id !== item.group_id) this.reset();
      this.current = item;
      this.items = items.filter(value => !value.deleted && !value.read_error);
      this.grouping = grouping;
      if (!this.items.some(value => value.index === this.referenceIndex)) this.referenceIndex = null;
      const reference = this.items.find(value => value.index === this.referenceIndex);
      this.dialog.classList.toggle("comparing", Boolean(reference));
      const compare = this.query("#dialog-compare");
      compare.disabled = this.items.length < 2;
      compare.textContent = reference ? "结束比较" : "并排比较";
      compare.setAttribute("aria-pressed", String(Boolean(reference)));
      this.query("#dialog-pin").hidden = !reference;
      this.query("#dialog-pin").disabled = reference?.index === item.index;
      if (reference) {
        const image = this.query("#dialog-reference-image");
        if (image.getAttribute("src") !== reference.preview_url) image.src = reference.preview_url;
        image.alt = reference.filename;
        this.query("#dialog-reference-name").textContent = `参照 · ${reference.filename}`;
        this.query("#dialog-reference-score").innerHTML = this.score(reference, grouping);
        this.query("#dialog-reference-ratings").innerHTML = grouping ? "" : this.ratings(reference, true);
      }
      this.query("#dialog-current-name").textContent = reference ? `当前 · ${item.filename}` : "";
      this.query("#dialog-current-score").innerHTML = reference ? this.score(item, grouping) : "";
      this.renderGeometry();
    }

    toggleCompare() {
      if (this.referenceIndex != null) {
        this.referenceIndex = null;
        this.selectPhoto(this.current.index);
      } else this.pinCurrent();
    }

    pinCurrent() {
      if (!this.current || this.items.length < 2) return;
      this.referenceIndex = this.current.index;
      const position = this.items.findIndex(item => item.index === this.current.index);
      const next = this.items[position + 1] || this.items[position - 1];
      this.selectPhoto(next.index);
    }

    setZoom(value) {
      if (!["fit", "1", "2"].includes(value)) return;
      this.zoom = value;
      if (value === "fit") this.center = {x: .5, y: .5};
      this.drag = null;
      this.renderGeometry();
    }

    renderGeometry() {
      for (const button of this.dialog.querySelectorAll("[data-inspection-zoom]")) {
        const active = button.dataset.inspectionZoom === this.zoom;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      }
      this.dialog.classList.toggle("inspection-zoomed", this.zoom !== "fit");
      for (const pane of this.panes) {
        const stage = pane.querySelector(".inspection-stage"), image = pane.querySelector("img");
        const geometry = layout(stage.clientWidth, stage.clientHeight, image.naturalWidth, image.naturalHeight, this.zoom, this.center);
        if (!geometry) continue;
        for (const [key, value] of Object.entries(geometry)) image.style[key] = `${value}px`;
      }
    }
  }
  PhotoReviewViewer.layout = layout;
  globalThis.PhotoReviewViewer = PhotoReviewViewer;
})();
