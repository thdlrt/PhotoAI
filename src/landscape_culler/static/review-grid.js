/* Preserve unaffected photo DOM while review state changes. */
(() => {
  class KeyedReviewGrid {
    constructor(container) {
      this.container = container;
      this.scope = null;
      this.sections = new Map();
    }

    render(scope, sections) {
      if (this.scope !== scope) {
        this.container.replaceChildren();
        this.sections.clear();
        this.scope = scope;
      }
      const focused = document.activeElement;
      const ratingFocus = this.container.contains(focused) && focused.dataset.rateIndex != null
        ? {index: focused.dataset.rateIndex, rating: focused.dataset.rating} : null;
      const keys = new Set(sections.map(section => section.key));
      for (const [key, value] of this.sections) {
        if (!keys.has(key)) {
          value.node.remove();
          this.sections.delete(key);
        }
      }
      const stats = {created: 0, updated: 0, reused: 0};
      sections.forEach(({key, html}, position) => {
        let stored = this.sections.get(key);
        if (!stored || stored.html !== html) {
          const template = document.createElement("template");
          template.innerHTML = html.trim();
          const node = template.content.firstElementChild;
          if (!node) return;
          if (stored) {
            // Whole cards are retained if unchanged; changed cards retain their
            // decoded image, avoiding a flash/reload after a rating edit.
            const previous = new Map([...stored.node.querySelectorAll(".photo-card[data-index]")]
              .map(card => [card.dataset.index, card]));
            for (const card of node.querySelectorAll(".photo-card[data-index]")) {
              const old = previous.get(card.dataset.index);
              if (!old) continue;
              if (old.isEqualNode(card)) card.replaceWith(old);
              else {
                const oldImage = old.querySelector(".preview-button img");
                const newImage = card.querySelector(".preview-button img");
                if (oldImage && newImage && oldImage.isEqualNode(newImage)) newImage.replaceWith(oldImage);
              }
            }
            if (node.tagName === "DETAILS" && stored.node.tagName === "DETAILS") node.open = stored.node.open;
            stored.node.replaceWith(node);
            stats.updated += 1;
          } else stats.created += 1;
          stored = {html, node};
          this.sections.set(key, stored);
        } else stats.reused += 1;
        const atPosition = this.container.children[position];
        if (atPosition !== stored.node) this.container.insertBefore(stored.node, atPosition || null);
      });
      if (ratingFocus && !focused.isConnected) {
        const button = [...this.container.querySelectorAll("[data-rate-index]")].find(node =>
          node.dataset.rateIndex === ratingFocus.index && node.dataset.rating === ratingFocus.rating);
        button?.focus({preventScroll: true});
      }
      return stats;
    }
  }
  globalThis.KeyedReviewGrid = KeyedReviewGrid;
})();
