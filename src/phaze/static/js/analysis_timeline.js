(function () {
    "use strict";

    const SELECTOR = "[data-analysis-timeline]";

    function clamp(value, minimum, maximum) {
        return Math.min(Math.max(value, minimum), maximum);
    }

    function elapsed(seconds) {
        const whole = Math.max(0, Math.round(seconds));
        const hours = Math.floor(whole / 3600);
        const minutes = Math.floor((whole % 3600) / 60);
        const remainder = whole % 60;
        if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
        return `${minutes}:${String(remainder).padStart(2, "0")}`;
    }

    function measuredWindow(windows, time, duration) {
        let low = 0;
        let high = windows.length - 1;
        let candidate = null;
        while (low <= high) {
            const middle = Math.floor((low + high) / 2);
            if (windows[middle].start <= time) {
                candidate = windows[middle];
                low = middle + 1;
            } else {
                high = middle - 1;
            }
        }
        if (!candidate) return null;
        const includesEnd = time === duration && time === candidate.end;
        return time < candidate.end || includesEnd ? candidate : null;
    }

    function range(window) {
        return `${elapsed(window.start)}–${elapsed(window.end)}`;
    }

    function fineDescription(window) {
        if (!window) return "Fine: no measured window";
        const values = [];
        if (window.bpm !== null) values.push(`BPM ${Number(window.bpm).toFixed(Number.isInteger(window.bpm) ? 0 : 1)}`);
        if (window.key) values.push(`key ${window.key}`);
        return `Fine ${range(window)}: ${values.length ? values.join(", ") : "no BPM or key value"}`;
    }

    function coarseDescription(window) {
        if (!window) return "Coarse: no measured window";
        const values = [];
        if (window.mood) values.push(`mood ${window.mood}`);
        if (window.style) values.push(`style ${window.style}`);
        return `Coarse ${range(window)}: ${values.length ? values.join(", ") : "no mood or style value"}`;
    }

    function initialize(root) {
        if (!(root instanceof HTMLElement) || root.dataset.timelineReady === "true") return;
        const payloadNode = root.querySelector("[data-timeline-inspection]");
        const inspector = root.querySelector("[data-timeline-inspector]");
        const viewport = root.querySelector("[data-timeline-viewport]");
        const frame = root.querySelector("[data-timeline-frame]");
        const readout = root.querySelector("[data-timeline-readout]");
        const tooltip = root.querySelector("[data-timeline-tooltip]");
        const cursor = root.querySelector("[data-timeline-cursor]");
        const hint = root.querySelector("[data-timeline-scroll-hint]");
        if (!payloadNode || !inspector || !viewport || !frame || !readout || !tooltip || !cursor || !hint) return;

        let payload;
        try {
            payload = JSON.parse(payloadNode.textContent || "{}");
        } catch (_error) {
            return;
        }
        const duration = Number(payload.duration);
        if (!Number.isFinite(duration) || duration <= 0 || !Array.isArray(payload.windows)) return;
        const validWindows = payload.windows.filter(
            (window) =>
                window &&
                Number.isFinite(window.start) &&
                Number.isFinite(window.end) &&
                window.start >= 0 &&
                window.end >= window.start,
        );
        const byTier = {
            fine: validWindows.filter((window) => window.tier === "fine").sort((left, right) => left.start - right.start),
            coarse: validWindows.filter((window) => window.tier === "coarse").sort((left, right) => left.start - right.start),
        };
        const stops = [...new Set([0, duration, ...validWindows.flatMap((window) => [window.start, (window.start + window.end) / 2, window.end])])]
            .filter((value) => Number.isFinite(value) && value >= 0 && value <= duration)
            .sort((left, right) => left - right);
        let currentTime = 0;

        function updateOverflow() {
            const tolerance = 2;
            const overflow = viewport.scrollWidth > viewport.clientWidth + tolerance;
            const canLeft = overflow && viewport.scrollLeft > tolerance;
            const canRight = overflow && viewport.scrollLeft + viewport.clientWidth < viewport.scrollWidth - tolerance;
            frame.classList.toggle("timeline-can-scroll-left", canLeft);
            frame.classList.toggle("timeline-can-scroll-right", canRight);
            hint.hidden = !overflow;
            if (canLeft && canRight) hint.textContent = "Scroll timeline ↔";
            else if (canLeft) hint.textContent = "← Scroll timeline";
            else hint.textContent = "Scroll timeline →";
        }

        function inspect(time, ensureVisible) {
            currentTime = clamp(time, 0, duration);
            const fine = measuredWindow(byTier.fine, currentTime, duration);
            const coarse = measuredWindow(byTier.coarse, currentTime, duration);
            const description = `At ${elapsed(currentTime)} · ${fineDescription(fine)} · ${coarseDescription(coarse)}`;
            const percent = (currentTime / duration) * 100;
            readout.textContent = description;
            tooltip.textContent = description;
            tooltip.hidden = false;
            tooltip.style.left = `${percent}%`;
            tooltip.dataset.side = percent < 18 ? "start" : percent > 82 ? "end" : "middle";
            cursor.hidden = false;
            cursor.style.left = `${percent}%`;
            inspector.setAttribute("aria-valuenow", currentTime.toFixed(3).replace(/\.0+$/, ""));
            inspector.setAttribute("aria-valuetext", description);
            if (ensureVisible) {
                const target = (currentTime / duration) * inspector.clientWidth;
                const margin = Math.min(80, viewport.clientWidth / 4);
                if (target < viewport.scrollLeft + margin) viewport.scrollTo({ left: Math.max(0, target - margin), behavior: "smooth" });
                if (target > viewport.scrollLeft + viewport.clientWidth - margin) {
                    viewport.scrollTo({ left: target - viewport.clientWidth + margin, behavior: "smooth" });
                }
            }
        }

        function pointerTime(event) {
            const bounds = inspector.getBoundingClientRect();
            return clamp((event.clientX - bounds.left) / bounds.width, 0, 1) * duration;
        }

        inspector.addEventListener("pointermove", (event) => inspect(pointerTime(event), false));
        inspector.addEventListener("pointerdown", (event) => inspect(pointerTime(event), false));
        inspector.addEventListener("focus", () => inspect(currentTime, true));
        inspector.addEventListener("keydown", (event) => {
            let next = null;
            if (event.key === "Home") next = 0;
            if (event.key === "End") next = duration;
            if (event.key === "ArrowRight") next = stops.find((stop) => stop > currentTime + 1e-6) ?? duration;
            if (event.key === "ArrowLeft") next = [...stops].reverse().find((stop) => stop < currentTime - 1e-6) ?? 0;
            if (next === null) return;
            event.preventDefault();
            inspect(next, true);
        });
        viewport.addEventListener("scroll", updateOverflow, { passive: true });
        const resizeObserver = new ResizeObserver(updateOverflow);
        resizeObserver.observe(viewport);
        root.__phazeTimelineResizeObserver = resizeObserver;
        root.dataset.timelineReady = "true";
        updateOverflow();
    }

    function initializeWithin(scope) {
        if (scope instanceof Element && scope.matches(SELECTOR)) initialize(scope);
        if (scope && scope.querySelectorAll) scope.querySelectorAll(SELECTOR).forEach(initialize);
    }

    window.PhazeAnalysisTimeline = { initialize: initializeWithin };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => initializeWithin(document), { once: true });
    else initializeWithin(document);
    document.body.addEventListener("htmx:afterSwap", (event) => initializeWithin(event.detail && event.detail.target));
})();
