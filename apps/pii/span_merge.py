"""Merge overflow windows without weakening the original truncated result."""


def _adjacent(end, start, text):
    return start <= end or (start == end + 1 and text[end:start].isspace())


def _overlaps(a, b):
    return a["start"] < b["end"] and b["start"] < a["end"]


def _context_rank(item):
    # Final keys make equal-context/equal-score choices independent of order.
    return (item.get("_context", 0), item["score"], item["entity_group"], -item["start"], -item["end"])


def _covered_windows(item, text):
    members = sorted(item["_members"], key=lambda s: (s["start"], s["end"]))
    cursors = {}
    for source in members:
        window = source.get("_window")
        if window is None or source["entity_group"] != item["entity_group"]:
            continue
        cursor = cursors.get(window, item["start"])
        if _adjacent(cursor, source["start"], text):
            cursors[window] = max(cursor, source["end"])
    return {window for window, end in cursors.items() if end >= item["end"]}


def _coobserved(a, b, text):
    # Preserve overlaps supported in full by one original decoder response.
    # A later extension from another window cannot inherit this exemption.
    return bool(_covered_windows(a, text) & _covered_windows(b, text))


def _safe_label_union(a, b, text):
    from apps.pii.config import DEBERTA_LABEL_MAP, LABEL_SCORE_OVERRIDES
    from apps.pii.hygiene import is_junk_span, validate_structured

    labels = {s["entity_group"] for s in a["_members"] + b["_members"]}
    types = {DEBERTA_LABEL_MAP.get(label) for label in labels}
    # Different canonical types have different entity allowlists, validators,
    # name allowlists, and junk filters. DATE/BUILDINGNUMBER are context gated;
    # PIN has a distinct score threshold. A winner must not change any gate.
    if len(types) != 1 or labels & ({"DATE", "BUILDINGNUMBER"} | LABEL_SCORE_OVERRIDES.keys()):
        return False
    entity_type = next(iter(types))
    if entity_type is None:
        return True  # Both labels were already ignored downstream.
    union = text[min(a["start"], b["start"]) : max(a["end"], b["end"])]
    if is_junk_span(union, entity_type)[0]:
        return False
    return entity_type in ("PERSON", "LOCATION") or validate_structured(entity_type, union)


def _union(a, b):
    members = a["_members"] + b["_members"]
    return {
        **max(members, key=_context_rank),
        "start": min(a["start"], b["start"]),
        "end": max(a["end"], b["end"]),
        "score": max(a["score"], b["score"]),
        "_members": members,
    }


def _merge_spans(spans, text, *, conflicts=None):
    """Union repeated spans, preserving overlaps that the decoder co-emitted.

    Cross-window disagreements choose the observation farther from its window
    edge, then higher score. Always retain the whole range and maximum confidence.
    Retain BOTH labels when a union would change downstream filtering. Optional
    conflict ranges let the caller choose the baseline-preserving fallback;
    downstream deduplication cannot safely consume these overlaps directly.
    Internal provenance never escapes.
    """
    components = []
    for raw in sorted(spans, key=lambda s: (s["entity_group"], s["start"], s["end"])):
        item = {**raw, "_members": [dict(raw)]}
        if (
            components
            and components[-1]["entity_group"] == item["entity_group"]
            and _adjacent(components[-1]["end"], item["start"], text)
        ):
            components[-1] = _union(components[-1], item)
        else:
            components.append(item)
    # Re-sort after each union: the expanded range may meet another component.
    while True:
        components.sort(key=lambda s: (s["start"], s["end"], s["entity_group"]))
        joined = False
        for i, a in enumerate(components):
            for j in range(i + 1, len(components)):
                b = components[j]
                if b["start"] > a["end"] + 1:
                    break
                same_label = a["entity_group"] == b["entity_group"]
                if same_label and _adjacent(a["end"], b["start"], text):
                    pass
                elif not _overlaps(a, b) or _coobserved(a, b, text):
                    continue
                elif not _safe_label_union(a, b, text):
                    if conflicts is not None:
                        conflicts.append((min(a["start"], b["start"]), max(a["end"], b["end"])))
                    continue
                components[i] = _union(a, b)
                del components[j]
                joined = True
                break
            if joined:
                break
        if not joined:
            break
    return [
        {
            "entity_group": item["entity_group"],
            "score": item["score"],
            "start": item["start"],
            "end": item["end"],
            "word": text[item["start"] : item["end"]],
        }
        for item in components
    ]


def _preserve_baseline(baseline, later, text):
    """Keep origin's exact spans, adding only independent overflow detections.

    Keeping both conflicting labels alone is unsafe: redactor._filter_results
    uses label-specific gates and _deduplicate_overlapping prefers shorter spans
    on ties. Even same-label extensions can change hygiene/allowlist decisions.
    Never modify baseline spans. On unsafe conflicts, discard later detections
    touching the conflict. Also isolate ALL additions from baseline and pattern
    ranges, including word snapping, adjacent-span merging and the two-character
    BUILDINGNUMBER context gate. This preserves the original downstream overlap
    groups for every entity tier/allowlist, not just their raw character union.
    """
    from apps.pii.engine import get_pattern_recognizers
    from apps.pii.hygiene import snap_to_word_boundaries
    from apps.pii.shared_client import MAX_SPANS

    conflicts = []
    merged = _merge_spans([{**s, "_window": 0} for s in baseline] + later, text, conflicts=conflicts)

    def envelope(start, end):
        # Trimming occurs before snapping downstream. Include both endpoints
        # and both expansions, conservatively even for non-name labels.
        value = text[start:end]
        trimmed_start = start + len(value) - len(value.lstrip())
        trimmed_end = end - len(value) + len(value.rstrip())
        a, b = snap_to_word_boundaries(text, start, end)
        c, d = snap_to_word_boundaries(text, trimmed_start, trimmed_end)
        return min(start, a, c), max(end, b, d)

    protected = [envelope(s["start"], s["end"]) for s in baseline]
    protected.extend(envelope(start, end) for start, end in conflicts)
    # Pattern recognizers run after the neural adapter in the redactor. A
    # pattern bridging otherwise disjoint neural spans can change the winner,
    # so reserve every match irrespective of the caller's tier/threshold.
    for entity_type, recognizer in get_pattern_recognizers().items():
        protected.extend(envelope(s.start, s.end) for s in recognizer.analyze(text=text, entities=[entity_type]))
    additions = []
    for span in merged:
        start, end = envelope(span["start"], span["end"])
        if any(start <= b + 2 and a <= end + 2 for a, b in protected):
            continue
        additions.append(span)
    # Keep exact baseline dictionaries and stable tie order while placing
    # additions before any full-text hybrid tail hits. Downstream uses the same
    # stable position sort; additions cannot interfere with protected groups.
    # Extra coverage must not turn a valid origin response into a whole-call
    # wire-cap failure. An already-oversize baseline keeps origin's behavior.
    additions = additions[: max(0, MAX_SPANS - len(baseline))]
    return sorted(list(baseline) + additions, key=lambda span: span["start"])
