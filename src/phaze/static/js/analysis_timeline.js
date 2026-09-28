(function () {
    "use strict";

    const SELECTOR = "[data-analysis-timeline]";
    // phaze-x1qr3.10: how far out the satellite targets may be looked up. The tracklist rows, the
    // Camelot wheel and the set glyph all live OUTSIDE the timeline's own subtree, so they cannot
    // be found with `root.querySelector`; a `document.querySelector` would find them anywhere on
    // the page, which is wrong on every surface that renders more than one file (the Files matrix
    // and Changes Review render a glyph per row). The record page and the record drawer each mark
    // their own container; a timeline with no such ancestor -- a proposal row -- drives its own
    // lanes and nothing else, which is exactly right because it has no satellites.
    const SCOPE_SELECTOR = "[data-inspection-scope]";

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

    // The pure lookups. Everything the inspection draws is decided by these three functions and
    // one elapsed time; nothing below them reads the DOM. They are exported on the public object
    // so `tests/browser/test_analysis_timeline_lookups.py` can unit-test them against the SHIPPED
    // file in a real browser -- this repo has no node or jsdom harness, and a Python port of the
    // arithmetic would be exactly the "proxy that cannot exhibit the failure" ADR-0012 (verification fidelity and operator attribution) is about.

    /** The last span starting at or before `time` whose half-open range still contains it.
     *
     * Half-open `[start, end)` everywhere, with ONE deliberate exception: a time exactly at the
     * file's own duration belongs to the span that ends there. Without it the very end of a file
     * -- which `End` and a pointer at the right edge both land on -- would report nothing measured
     * at all, on a file that is measured right up to that instant.
     *
     * A `null` or absent `end` means "runs to the end of what we know" and is bounded by
     * `duration`. Only a track segment can carry one (the last track of a file whose duration we
     * never learned); windows and key runs always end somewhere.
     */
    function spanAt(spans, time, duration) {
        let low = 0;
        let high = spans.length - 1;
        let candidate = null;
        while (low <= high) {
            const middle = Math.floor((low + high) / 2);
            if (spans[middle].start <= time) {
                candidate = spans[middle];
                low = middle + 1;
            } else {
                high = middle - 1;
            }
        }
        if (!candidate) return null;
        const end = candidate.end === null || candidate.end === undefined ? duration : candidate.end;
        const includesEnd = time === duration && time === end;
        return time < end || includesEnd ? candidate : null;
    }

    /** The measured window of one tier at `time`, or null inside a coverage gap. */
    function measuredWindow(windows, time, duration) {
        return spanAt(windows, time, duration);
    }

    /** The track whose scraped span contains `time`, or null before the first track / in a gap.
     *
     * Returns null for a zero-length segment too, which is what a non-monotonic pair of scraped
     * timestamps collapses to: the reader is told nothing rather than told a boundary we do not
     * trust.
     */
    function segmentAt(segments, time, duration) {
        return spanAt(segments, time, duration);
    }

    /** The flicker-filtered key run containing `time`, or null where no key was measured.
     *
     * The returned run's `index` is the wheel's own `data-node-index` -- both sides enumerate
     * `set_projection.placeable_key_runs`, so the ring lands on the node the run was drawn as.
     */
    function keyRunAt(runs, time, duration) {
        return spanAt(runs, time, duration);
    }

    // Phrases. One vocabulary for the readout, the tooltip and `aria-valuetext`, so a screen
    // reader hears the same six facts -- time, BPM, track, energy, key, mood -- the tooltip shows.

    function range(window) {
        return `${elapsed(window.start)}–${elapsed(window.end)}`;
    }

    function formatBpm(bpm) {
        return Number(bpm).toFixed(Number.isInteger(bpm) ? 0 : 1);
    }

    function fineDescription(window) {
        if (!window) return "Fine: no measured window";
        const values = [];
        if (window.bpm !== null) values.push(`BPM ${formatBpm(window.bpm)}`);
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

    function trackPhrase(segment) {
        if (!segment) return "no track";
        return segment.title ? `track ${segment.position} ${segment.title}` : `track ${segment.position}`;
    }

    /** The fine window's BPM as the tooltip/readout headline states it, or an explicit absence.
     *
     * An absent BPM (no fine window measured here, or the measured one carries no BPM) reads
     * "BPM —" -- the same em dash the gutter's own scale shows when it has no bounds -- rather
     * than being silently dropped from the headline or reading "BPM undefined".
     */
    function bpmPhrase(fine) {
        if (!fine || fine.bpm === null || fine.bpm === undefined) return "BPM —";
        return `BPM ${formatBpm(fine.bpm)}`;
    }

    function keyPhrase(fine) {
        if (!fine || !fine.key) return "no key";
        return fine.camelot ? `key ${fine.key} · ${fine.camelot}` : `key ${fine.key}`;
    }

    function moodPhrase(coarse) {
        const top = coarse && coarse.mood_top;
        if (!top) return "no mood";
        return `mood ${top.label} ${Math.round(top.share * 100)}%`;
    }

    function energyPhrase(coarse) {
        if (!coarse || coarse.energy === null || coarse.energy === undefined) return "no energy";
        return `energy ${Number(coarse.energy).toFixed(2)}`;
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
        const segments = (Array.isArray(payload.segments) ? payload.segments : [])
            .filter((segment) => segment && Number.isFinite(segment.start) && segment.start >= 0)
            .sort((left, right) => left.start - right.start);
        const keyRuns = (Array.isArray(payload.key_runs) ? payload.key_runs : [])
            .filter((run) => run && Number.isFinite(run.start) && Number.isFinite(run.end))
            .sort((left, right) => left.start - right.start);
        // Where the page rests with nothing hovered. `null` -- no coarse window carried a measured
        // energy -- rests at zero and says so plainly; it never labels zero "peak", which would be
        // a claim about a maximum nothing measured.
        const peakSec = Number.isFinite(payload.peak_sec) ? clamp(Number(payload.peak_sec), 0, duration) : null;
        const stops = [...new Set([0, duration, ...validWindows.flatMap((window) => [window.start, (window.start + window.end) / 2, window.end])])]
            .filter((value) => Number.isFinite(value) && value >= 0 && value <= duration)
            .sort((left, right) => left - right);

        // The satellites. Each is optional and independently absent: the drawer has no wheel and
        // no glyph, a proposal row has none of the three, and a file with no tracklist has rows
        // nowhere. Absent means the corresponding mark simply never lights.
        const scope = root.closest(SCOPE_SELECTOR);
        const trackSpan = root.querySelector("[data-timeline-track-span]");
        const keyRibbons = [...root.querySelectorAll('[data-timeline-lane="key"] .analysis-timeline-ribbon')];
        const bpmPlot = root.querySelector("[data-timeline-bpm-plot]");
        const bpmMarker = root.querySelector("[data-timeline-bpm-marker]");
        const bpmMarkerLabel = bpmMarker ? bpmMarker.querySelector("[data-timeline-bpm-marker-label]") : null;
        // Undefined on a file with no valid BPM anywhere -- `bpm_spark`/`bpm_segments` render no
        // scale in that case either (`analysis_timeline.html`'s own `data-bpm-lo`/`data-bpm-hi`
        // guard), so `null` here and an absent marker are the same claim made twice.
        const bpmLo = bpmPlot && bpmPlot.dataset.bpmLo !== undefined ? Number(bpmPlot.dataset.bpmLo) : null;
        const bpmHi = bpmPlot && bpmPlot.dataset.bpmHi !== undefined ? Number(bpmPlot.dataset.bpmHi) : null;
        const tracklist = scope ? scope.querySelector("[data-tracklist-index]") : null;
        const wheelRing = scope ? scope.querySelector("[data-journey-cursor-ring]") : null;
        // The ring is only rendered on a wheel that has a journey, so a wheel with no nodes to
        // hover has no ring and is never listened to either.
        const wheel = wheelRing ? wheelRing.closest("[data-harmonic-wheel]") : null;
        const glyph = scope ? scope.querySelector("[data-set-glyph]") : null;
        const glyphCursor = scope ? scope.querySelector("[data-set-glyph-cursor]") : null;

        let currentTime = peakSec === null ? 0 : peakSec;

        // The satellite node lists, resolved once and cached. `markTracklistRow` and `markGlyph`
        // run on EVERY pointer sample, and each re-ran a `querySelectorAll` over the whole
        // tracklist / the whole glyph to do it -- a full DOM walk per sample on a surface whose
        // tracklist is routinely 40-60 rows (PR #556 review, finding 7).
        //
        // Lazily rather than at init, because htmx swaps the tracklist's INNER
        // `#tracklist-review-<id>` while these listeners stay delegated on the section that
        // survives: rows resolved at init would be detached nodes after the first Prioritize or
        // Refresh, and would silently stop highlighting. `htmx:afterSwap` bubbles out of the
        // swapped element, so a listener on the section sees every swap that can replace them
        // and the next sample re-resolves.
        let trackRows = null;
        let glyphCellCount = null;

        function trackRowNodes() {
            if (trackRows === null) trackRows = [...tracklist.querySelectorAll("[data-track-row]")];
            return trackRows;
        }

        function glyphCells() {
            if (glyphCellCount === null) glyphCellCount = glyph ? glyph.querySelectorAll("[data-glyph-cell]").length : 0;
            return glyphCellCount;
        }

        if (tracklist) tracklist.addEventListener("htmx:afterSwap", () => (trackRows = null));
        if (scope) scope.addEventListener("htmx:afterSwap", () => (glyphCellCount = null));

        function updateOverflow() {
            const tolerance = 2;
            // `scrollWidth - clientWidth` is NOT the maximum scroll offset when the scroller reserves a
            // scrollbar gutter: the gutter is excluded from clientWidth yet cannot be scrolled into, so
            // the difference overstates the maximum by the gutter width wherever scrollbars are classic
            // (Linux, Windows) while reading 0 on macOS overlay scrollbars. Scrolled fully right, the
            // naive form therefore left `canRight` true forever -- the right-edge fade lit and the hint
            // reading "<->" instead of "<-". The viewport is border-less by design, so the difference
            // between its border box and its content box is exactly that reserved gutter.
            const gutter = Math.max(0, viewport.offsetWidth - viewport.clientWidth);
            const maxScroll = viewport.scrollWidth - viewport.clientWidth - gutter;
            const overflow = maxScroll > tolerance;
            const canLeft = overflow && viewport.scrollLeft > tolerance;
            const canRight = overflow && viewport.scrollLeft < maxScroll - tolerance;
            frame.classList.toggle("timeline-can-scroll-left", canLeft);
            frame.classList.toggle("timeline-can-scroll-right", canRight);
            hint.hidden = !overflow;
            if (canLeft && canRight) hint.textContent = "Scroll timeline ↔";
            else if (canLeft) hint.textContent = "← Scroll timeline";
            else hint.textContent = "Scroll timeline →";
        }

        /** The dot and label at the cursor's fine BPM, or hidden where there is nothing to plot.
         *
         * `percent` is the SAME horizontal fraction the cursor and tooltip already use, so the
         * dot sits on the cursor line rather than drifting to its own x. The vertical fraction
         * mirrors `bpm_spark`/`bpm_segments` exactly -- `(hi - bpm) / (hi - lo)`, higher BPM
         * higher on the chart -- so the dot lands on the plotted line, not merely near it.
         */
        function markBpmMarker(fine, percent) {
            if (!bpmMarker) return;
            const bpm = fine && fine.bpm !== null && fine.bpm !== undefined ? Number(fine.bpm) : null;
            if (bpm === null || bpmLo === null || bpmHi === null || bpmHi === bpmLo) {
                bpmMarker.hidden = true;
                return;
            }
            const fraction = clamp((bpmHi - bpm) / (bpmHi - bpmLo), 0, 1);
            bpmMarker.hidden = false;
            bpmMarker.style.left = `${percent}%`;
            bpmMarker.style.top = `${fraction * 100}%`;
            if (bpmMarkerLabel) bpmMarkerLabel.textContent = `BPM ${formatBpm(bpm)}`;
        }

        function markTrackSpan(segment) {
            if (!trackSpan) return;
            if (!segment) {
                trackSpan.hidden = true;
                return;
            }
            const end = segment.end === null || segment.end === undefined ? duration : segment.end;
            trackSpan.hidden = false;
            trackSpan.style.left = `${(segment.start / duration) * 100}%`;
            trackSpan.style.width = `${((clamp(end, segment.start, duration) - segment.start) / duration) * 100}%`;
        }

        function markKeyRibbon(time) {
            for (const ribbon of keyRibbons) {
                const start = Number(ribbon.dataset.ribbonStart);
                const end = Number(ribbon.dataset.ribbonEnd);
                // Same half-open rule, and the same file-end exception, as `spanAt` -- a ribbon
                // and the window it was drawn from must light and read at exactly the same
                // instants, or the outline names one key while the readout names another.
                const inside = Number.isFinite(start) && Number.isFinite(end) && start <= time && (time < end || (time === duration && time === end));
                ribbon.classList.toggle("is-current", inside);
            }
        }

        function markTracklistRow(segment) {
            if (!tracklist) return;
            for (const row of trackRowNodes()) {
                row.classList.toggle("is-current", Boolean(segment) && row.dataset.trackPosition === String(segment.position));
            }
        }

        function markWheel(run) {
            if (!wheelRing) return;
            const node = run && wheel ? wheel.querySelector(`[data-journey-node][data-node-index="${run.index}"]`) : null;
            // The ATTRIBUTE, never `wheelRing.hidden`. The ring is an SVG <circle>, and
            // SVGElement has no `hidden` IDL property: assigning one only sets a JS expando, the
            // server-rendered `hidden` attribute stays, and `[hidden] { display: none }` keeps
            // the ring invisible while every read of `.hidden` reports it shown (phaze-n0h86).
            wheelRing.toggleAttribute("hidden", !node);
            if (!node) return;
            // The ring reads the NODE's own geometry rather than recomputing a wheel position, so
            // it cannot drift off the dot it is ringing however the wheel's radii change.
            wheelRing.setAttribute("cx", node.getAttribute("cx"));
            wheelRing.setAttribute("cy", node.getAttribute("cy"));
            wheelRing.setAttribute("r", String(Number(node.getAttribute("r")) + 4));
        }

        function markGlyph(coarse) {
            if (!glyphCursor) return;
            const cells = glyphCells();
            const ordinal = coarse ? byTier.coarse.indexOf(coarse) : -1;
            // The glyph's horizontal axis is the coarse window ORDINAL, not elapsed time, so the
            // marker is placed by ordinal. A time inside a coarse coverage hole marks nothing --
            // there is no cell for it, and marking the nearest one would claim a window the
            // cursor is not in.
            if (ordinal < 0 || cells === 0 || ordinal >= cells) {
                glyphCursor.hidden = true;
                return;
            }
            glyphCursor.hidden = false;
            glyphCursor.style.left = `${((ordinal + 0.5) / cells) * 100}%`;
        }

        function inspect(time, ensureVisible, resting) {
            // Any direct inspect supersedes a deferred pointer sample: without this, a hover
            // queued in the frame the pointer LEFT would land after `rest()` and leave the page
            // reading a position the cursor is no longer at.
            cancelQueuedSample();
            currentTime = clamp(time, 0, duration);
            const fine = measuredWindow(byTier.fine, currentTime, duration);
            const coarse = measuredWindow(byTier.coarse, currentTime, duration);
            const segment = segmentAt(segments, currentTime, duration);
            const run = keyRunAt(keyRuns, currentTime, duration);
            const headline = [
                `At ${elapsed(currentTime)}`,
                bpmPhrase(fine),
                trackPhrase(segment),
                energyPhrase(coarse),
                keyPhrase(fine),
                moodPhrase(coarse),
            ].join(" · ");
            // The readout and `aria-valuetext` carry the headline AND both tier descriptions, so
            // the screen-reader sentence is never shorter than what a sighted reader can see; the
            // tooltip carries the headline alone, because it sits over the picture it describes.
            const description = `${headline} · ${fineDescription(fine)} · ${coarseDescription(coarse)}`;
            const percent = (currentTime / duration) * 100;
            readout.textContent = description;
            tooltip.textContent = resting && peakSec !== null ? `Peak · ${headline}` : headline;
            tooltip.hidden = false;
            tooltip.style.left = `${percent}%`;
            tooltip.dataset.side = percent < 18 ? "start" : percent > 82 ? "end" : "middle";
            cursor.hidden = false;
            cursor.style.left = `${percent}%`;
            inspector.setAttribute("aria-valuenow", currentTime.toFixed(3).replace(/\.0+$/, ""));
            inspector.setAttribute("aria-valuetext", description);
            markTrackSpan(segment);
            markKeyRibbon(currentTime);
            markTracklistRow(segment);
            markWheel(run);
            markGlyph(coarse);
            markBpmMarker(fine, percent);
            if (ensureVisible) {
                const target = (currentTime / duration) * inspector.clientWidth;
                const margin = Math.min(80, viewport.clientWidth / 4);
                if (target < viewport.scrollLeft + margin) viewport.scrollTo({ left: Math.max(0, target - margin), behavior: "smooth" });
                if (target > viewport.scrollLeft + viewport.clientWidth - margin) {
                    viewport.scrollTo({ left: target - viewport.clientWidth + margin, behavior: "smooth" });
                }
            }
        }

        /** Nothing hovered: the page rests on the set's peak (or on zero, when nothing measured). */
        function rest() {
            inspect(peakSec === null ? 0 : peakSec, false, true);
        }

        // ONE inspect per animation frame for pointer MOTION. A drag delivers a sample per input
        // event and each one re-marks the ribbons, the tracklist rows and the glyph cursor;
        // every sample past the first in a frame is overwritten before anything is painted
        // (PR #556 review, finding 7).
        //
        // LEADING EDGE SYNCHRONOUS, trailing edge on the next frame. The first sample after a
        // pause applies immediately -- no added latency, and nothing reading the DOM straight
        // after a single pointer move can observe a stale value -- while samples arriving inside
        // the same frame are collapsed to the last one, which is the only one that would have
        // been visible anyway. Wrapping every sample in `requestAnimationFrame` instead defers
        // that first one too, for a frame of lag and no gain.
        //
        // The elapsed-time marker is `performance.now()` and NOT
        // `document.timeline.currentTime`, which reads like the natural frame identity and is
        // the wrong one: it only advances when a frame is actually produced, so on an idle page
        // two samples SECONDS apart carry the identical timestamp and the second is deferred as
        // though it were a duplicate. Measured in this repo's headless harness -- two evaluates
        // with a real pointer move between them both read 392.032 -- which is what turned three
        // existing browser tests red.
        const FRAME_MS = 16;
        let lastSampleAt = Number.NEGATIVE_INFINITY;
        let queuedTime = null;
        let queuedFrame = 0;

        function cancelQueuedSample() {
            if (queuedFrame) cancelAnimationFrame(queuedFrame);
            queuedFrame = 0;
            queuedTime = null;
        }

        function flushQueuedSample() {
            queuedFrame = 0;
            if (queuedTime === null) return;
            const time = queuedTime;
            queuedTime = null;
            lastSampleAt = performance.now();
            inspect(time, false, false);
        }

        function inspectFromPointer(time) {
            const now = performance.now();
            if (now - lastSampleAt < FRAME_MS) {
                queuedTime = time;
                if (!queuedFrame) queuedFrame = requestAnimationFrame(flushQueuedSample);
                return;
            }
            lastSampleAt = now;
            inspect(time, false, false);
        }

        function pointerTime(event) {
            const bounds = inspector.getBoundingClientRect();
            return clamp((event.clientX - bounds.left) / bounds.width, 0, 1) * duration;
        }

        /** The midpoint of the coarse window the pointer is over on the GLYPH.
         *
         * The glyph is indexed by coarse ordinal, so a fraction of its width is a cell, not an
         * elapsed time -- reading it as a time would put the cursor in the wrong window on any
         * file whose coarse coverage has a hole.
         */
        function glyphTime(event) {
            const cells = glyphCells();
            if (!cells) return null;
            const bounds = glyph.getBoundingClientRect();
            const ordinal = Math.min(cells - 1, Math.floor(clamp((event.clientX - bounds.left) / bounds.width, 0, 0.999999) * cells));
            const window = byTier.coarse[ordinal];
            return window ? (window.start + window.end) / 2 : null;
        }

        function rowTime(row) {
            const start = Number(row.dataset.trackStart);
            if (!Number.isFinite(start)) return null;
            const end = Number(row.dataset.trackEnd);
            return (start + (Number.isFinite(end) ? clamp(end, start, duration) : duration)) / 2;
        }

        /** The midpoint of the key run an element carries `data-node-index` for, or null if
         * unknown -- a wheel node's `<circle>` and, since phaze-37ovq, its runs-table `<tr>`.
         *
         * Looked up by `index`, the same numbering `markWheel` rings by, so hovering EITHER
         * element for a run lands the cursor inside that run and rings its wheel node -- never a
         * sibling visit to the same Camelot position, which is a different node with a different
         * index.
         */
        function nodeTime(node) {
            const index = Number(node.dataset.nodeIndex);
            const run = keyRuns.find((candidate) => candidate.index === index);
            return run ? (clamp(run.start, 0, duration) + clamp(run.end, run.start, duration)) / 2 : null;
        }

        inspector.addEventListener("pointermove", (event) => inspectFromPointer(pointerTime(event)));
        inspector.addEventListener("pointerdown", (event) => inspect(pointerTime(event), false, false));
        inspector.addEventListener("pointerleave", rest);
        inspector.addEventListener("focus", () => inspect(currentTime, true, false));
        // Deliberately on the INSPECTOR, never on `document`. A global keydown handler would take
        // Left/Right away from every text field, dialog and native control on the page and would
        // shadow the browser's own shortcuts; the inspector is a `role="slider"` with `tabindex`,
        // so arrows reach it exactly when it is the focused control and never otherwise.
        inspector.addEventListener("keydown", (event) => {
            let next = null;
            if (event.key === "Home") next = 0;
            if (event.key === "End") next = duration;
            if (event.key === "ArrowRight") next = stops.find((stop) => stop > currentTime + 1e-6) ?? duration;
            if (event.key === "ArrowLeft") next = [...stops].reverse().find((stop) => stop < currentTime - 1e-6) ?? 0;
            if (next === null) return;
            event.preventDefault();
            inspect(next, true, false);
        });

        if (glyph) {
            glyph.addEventListener("pointermove", (event) => {
                const time = glyphTime(event);
                if (time !== null) inspectFromPointer(time);
            });
            glyph.addEventListener("pointerleave", rest);
        }

        // Delegated on the tracklist SECTION, which htmx never replaces -- the Prioritize/Refresh
        // responses swap the inner `#tracklist-review-<id>` only, so rows arriving from a swap are
        // driven by these same listeners with no re-initialisation.
        if (tracklist) {
            const rowOf = (event) => (event.target instanceof Element ? event.target.closest("[data-track-row]") : null);
            const enter = (event) => {
                const row = rowOf(event);
                if (!row) return;
                const time = rowTime(row);
                if (time !== null) inspect(time, event.type === "focusin", false);
            };
            tracklist.addEventListener("mouseover", enter);
            tracklist.addEventListener("focusin", enter);
            tracklist.addEventListener("mouseleave", rest);
            tracklist.addEventListener("focusout", (event) => {
                if (!tracklist.contains(event.relatedTarget)) rest();
            });
        }

        // phaze-n0h86: the wheel drives the timeline back. Same shape as the tracklist -- delegated
        // on the <svg>, hover and focus inspect the node's run, leaving rests -- so a node is one
        // more route to the one elapsed time rather than a second cursor. The drawer renders no
        // wheel, so `wheel` is null there and nothing is attached.
        if (wheel) {
            const nodeOf = (event) => (event.target instanceof Element ? event.target.closest("[data-journey-node][data-node-index]") : null);
            const enter = (event) => {
                const node = nodeOf(event);
                if (!node) return;
                const time = nodeTime(node);
                if (time !== null) inspect(time, event.type === "focusin", false);
            };
            wheel.addEventListener("mouseover", enter);
            wheel.addEventListener("focusin", enter);
            wheel.addEventListener("mouseleave", rest);
            wheel.addEventListener("focusout", (event) => {
                if (!wheel.contains(event.relatedTarget)) rest();
            });
        }

        // phaze-37ovq: the harmonic journey's runs table drives the timeline too -- same shape as
        // the wheel it sits beside, and deliberately reusing `nodeTime` rather than a parallel
        // lookup: a table row carries the identical `data-node-index` a wheel node does, so
        // hovering a row lands the cursor in the same run hovering its node would, through the
        // one shared function. `wheel` (not `scope`) gates this listener too, matching the wheel's
        // own null-on-the-drawer behaviour -- the runs table only ever renders beside a wheel.
        //
        // Pointer (`mouseover`/`mouseleave`) ONLY, deliberately no `focusin`/`focusout`: rows
        // carry no `tabindex` (see the template), because the wheel's own nodes are already the
        // keyboard route to every run and its own accessible name, and a second set of tab stops
        // immediately after them would break phaze-n0h86's "tabbing out of the wheel rests the
        // page" contract.
        const runsTable = wheel ? scope.querySelector("[data-harmonic-runs]") : null;
        if (runsTable) {
            const rowOf = (event) => (event.target instanceof Element ? event.target.closest("[data-journey-row][data-node-index]") : null);
            runsTable.addEventListener("mouseover", (event) => {
                const row = rowOf(event);
                if (!row) return;
                const time = nodeTime(row);
                if (time !== null) inspect(time, false, false);
            });
            runsTable.addEventListener("mouseleave", rest);
        }

        viewport.addEventListener("scroll", updateOverflow, { passive: true });
        const resizeObserver = new ResizeObserver(updateOverflow);
        resizeObserver.observe(viewport);
        root.__phazeTimelineResizeObserver = resizeObserver;
        root.dataset.timelineReady = "true";
        updateOverflow();
        rest();
    }

    function initializeWithin(scope) {
        if (scope instanceof Element && scope.matches(SELECTOR)) initialize(scope);
        if (scope && scope.querySelectorAll) scope.querySelectorAll(SELECTOR).forEach(initialize);
    }

    window.PhazeAnalysisTimeline = { initialize: initializeWithin, measuredWindow, segmentAt, keyRunAt };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => initializeWithin(document), { once: true });
    else initializeWithin(document);
    document.body.addEventListener("htmx:afterSwap", (event) => initializeWithin(event.detail && event.detail.target));
})();
