// phaze-dwevc: THE count formatter for the browser -- the byte-for-byte twin of `phaze.utils.humanize.format_count`
// (the `thousands` Jinja filter). Every Alpine `x-text` / `:title` / `:aria-label` that renders a count calls
// `formatCount(...)`, so a live-updated value keeps its separators after a poll swaps it (145057 -> 145,057).
// tests/browser/test_format_count_parity.py runs both implementations over one case table; keep them in step.
//
// Grouping is a regex rather than the browser's locale-aware number formatting, so the output never depends on the
// operator's locale and cannot disagree with the server's `f"{n:,}"`.
(function () {
    "use strict";

    var NO_DATA = "—";
    var INTEGER_TEXT = /^[+-]?\d+$/;

    function group(digits) {
        return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    }

    function formatCount(value) {
        if (value === null || value === undefined || typeof value === "boolean") return NO_DATA;
        var number;
        if (typeof value === "number") {
            number = value;
        } else if (typeof value === "string") {
            var text = value.trim();
            if (!INTEGER_TEXT.test(text)) return value;
            number = Number(text);
        } else {
            return String(value);
        }
        if (!isFinite(number)) return NO_DATA;
        if (!Number.isInteger(number)) return String(number);
        return (number < 0 ? "-" : "") + group(String(Math.abs(number)));
    }

    window.formatCount = formatCount;
})();
