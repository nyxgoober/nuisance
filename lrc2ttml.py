#!/usr/bin/env python3
"""
lrc2ttml.py — LRC & Enhanced LRC parser and TTML converter.
Supports standard LRC, Enhanced LRC (<mm:ss.xx> word/syllable tags),
metadata tags ([ti:], [ar:], [al:], [length:], [offset:]), multi-timestamp
chorus lines, empty-line break markers, and co-timed bilingual/romaji lines.
"""

import argparse
import os
import re
import sys

# ── file decoding ──────────────────────────────────────────────

def read_text_file(path):
    """Read a text file handling UTF-8 (with/without BOM), UTF-16, Shift-JIS, GB18030."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp932", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ── time helpers ───────────────────────────────────────────────

_TS_RE = re.compile(
    r"^\s*(?:(\d+):)?(\d+):(\d+)(?:([.:])(\d+))?\s*$"
)

def parse_lrc_ts(ts):
    """
    Parse an LRC timestamp string into seconds (float), or None if invalid.
    Accepts:
      mm:ss.xx, mm:ss.xxx, mm:ss.x, mm:ss
      mm:ss:xx (colon before centiseconds)
      hh:mm:ss.xxx, hh:mm:ss
    """
    m = _TS_RE.match(ts)
    if not m:
        return None
    g1, g2, g3, sep, frac = m.groups()
    if g1 is not None and sep is None:
        # Three colon-separated groups with no fractional part:
        # In LRC tools [mm:ss:cc] uses 2-digit centiseconds if g1 < 60 and g3 <= 99,
        # unless g2 >= 60 or hours format [hh:mm:ss]. Disambiguate cleanly:
        a, b, c = int(g1), int(g2), int(g3)
        if len(g3) == 2 and a == 0 and b < 60:
            # Ambiguous 00:mm:ss vs 00:ss:cc — treat 00:mm:ss if c < 60?
            # Standard LRC [mm:ss:cc] has 2 digits at end; [hh:mm:ss] usually appears in [length:]
            return a * 60 + b + float("0." + g3)
        return a * 3600 + b * 60 + c

    hours = int(g1) if g1 is not None else 0
    minutes = int(g2)
    seconds = int(g3)
    sub = float("0." + frac) if frac else 0.0
    return hours * 3600 + minutes * 60 + seconds + sub


def fmt_ttml(sec):
    """Format seconds as HH:MM:SS.mmm without millisecond rollover bugs."""
    total_ms = max(0, round(float(sec) * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def esc(s):
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── LRC & Enhanced LRC parser ──────────────────────────────────

LINE_TAG = re.compile(r"\[(\d+:\d+(?:[.:]\d+){0,2})\]")
WORD_TAG = re.compile(r"<(\d+:\d+(?:[.:]\d+){0,2})>")
META_TAG = re.compile(r"^\[([a-zA-Z][a-zA-Z0-9_-]*):(.*)\]$")


def _shift_ts(sec, offset_sec, delta=0.0):
    return max(0.0, sec + delta - offset_sec)


def _parse_line_words(content, begin, default_end, offset_sec, delta):
    """
    Parse line content (plain or Enhanced LRC with <timestamp> tags).
    Returns (words, effective_end, is_synced).
    """
    word_tags = list(WORD_TAG.finditer(content))
    if not word_tags:
        text = content.strip()
        if not text:
            return [], default_end, False
        return [{"text": text, "begin": begin, "end": default_end}], default_end, False

    words = []
    # Capture any text appearing before the first <timestamp> tag
    lead_text = content[: word_tags[0].start()]
    first_wt = parse_lrc_ts(word_tags[0].group(1))
    first_w_begin = (
        _shift_ts(first_wt, offset_sec, delta) if first_wt is not None else begin
    )
    if lead_text.strip():
        w_end = max(begin, first_w_begin)
        words.append({"text": lead_text, "begin": begin, "end": w_end})

    effective_end = default_end
    for wi, wt in enumerate(word_tags):
        raw_t = parse_lrc_ts(wt.group(1))
        w_begin = _shift_ts(raw_t, offset_sec, delta) if raw_t is not None else begin
        text_start = wt.end()
        is_last = wi + 1 >= len(word_tags)
        text_end = word_tags[wi + 1].start() if not is_last else len(content)
        seg_text = content[text_start:text_end]

        if not is_last:
            nxt_raw = parse_lrc_ts(word_tags[wi + 1].group(1))
            w_end = (
                _shift_ts(nxt_raw, offset_sec, delta)
                if nxt_raw is not None
                else default_end
            )
        else:
            w_end = default_end

        w_end = max(w_begin, w_end)

        if seg_text.strip():
            words.append({"text": seg_text, "begin": w_begin, "end": w_end})
        elif seg_text and words and not is_last:
            # Timed whitespace/pause between words: preserve spacing on previous word
            words[-1]["text"] += seg_text
        elif is_last and not seg_text.strip():
            # Trailing end-of-line <timestamp> tag marks exact end of the phrase
            if w_begin > begin:
                effective_end = min(default_end, w_begin) if default_end > begin else w_begin

    if words:
        effective_end = max(effective_end, words[-1]["end"])
    return words, effective_end, True


def is_enhanced_lrc(text):
    """Return True if LRC text contains Enhanced LRC <timestamp> word tags."""
    return WORD_TAG.search(text) is not None


def parse_lrc(text):
    """
    Parse LRC or Enhanced LRC text.
    Returns (meta_dict, lines_list) where each item in lines_list is:
      {
        "begin": float,
        "end": float,
        "words": [{"text": str, "begin": float, "end": float}, ...],
        "synced": bool,          # True if Enhanced LRC word tags were present
        "sub_words": [...],      # Optional co-timed secondary line (romaji/translation)
        "sub_synced": bool,
      }
    Also sets meta["_duration"] (float seconds) if [length: ...] is present.
    """
    if text.startswith("\ufeff"):
        text = text[1:]

    meta = {}
    raw_entries = []  # (raw_time, delta_for_word_tags, content, order_idx)
    order_idx = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # Check line timestamps first so [00:12.00] isn't confused with metadata
        tags = list(LINE_TAG.finditer(line))
        if tags and tags[0].start() == 0:
            # Ensure all leading bracket tags are contiguous at the start
            content = line[tags[-1].end() :]
            parsed_times = []
            for tm in tags:
                t = parse_lrc_ts(tm.group(1))
                if t is not None:
                    parsed_times.append(t)
            if parsed_times:
                base_t = parsed_times[0]
                for t in parsed_times:
                    raw_entries.append((t, t - base_t, content, order_idx))
                    order_idx += 1
                continue

        mm = META_TAG.match(line)
        if mm:
            key = mm.group(1).strip().lower()
            val = mm.group(2).strip()
            meta[key] = val
            continue

    # Global [offset:+/-ms] adjustment (official spec: + shifts lyrics earlier)
    offset_sec = 0.0
    if "offset" in meta:
        try:
            offset_sec = float(meta["offset"]) / 1000.0
        except ValueError:
            offset_sec = 0.0

    # Optional [length: mm:ss] metadata
    if "length" in meta:
        dur_val = parse_lrc_ts(meta["length"])
        if dur_val is None:
            # Handle plain hh:mm:ss in [length: 00:03:45]
            parts = meta["length"].strip().split(":")
            try:
                if len(parts) == 3:
                    dur_val = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                elif len(parts) == 2:
                    dur_val = int(parts[0]) * 60 + float(parts[1])
            except ValueError:
                dur_val = None
        if dur_val is not None and dur_val > 0:
            meta["_duration"] = dur_val

    # Apply offset and sort stably by (timestamp, original line order)
    entries = [
        (_shift_ts(t, offset_sec), delta, content, idx)
        for (t, delta, content, idx) in raw_entries
    ]
    entries.sort(key=lambda x: (round(x[0], 3), x[3]))

    # Group co-timed lines (within 2ms) so dual-line LRCs don't get 0 duration
    groups = []
    for t, delta, content, _ in entries:
        if groups and abs(t - groups[-1]["begin"]) <= 0.002:
            groups[-1]["items"].append((t, delta, content))
        else:
            groups.append({"begin": t, "items": [(t, delta, content)]})

    track_dur = meta.get("_duration")
    result = []

    for gi, grp in enumerate(groups):
        begin = grp["begin"]
        if gi + 1 < len(groups):
            default_end = groups[gi + 1]["begin"]
        elif track_dur and track_dur > begin:
            default_end = min(track_dur, begin + 8.0)
        else:
            default_end = begin + 5.0

        parsed_lines = []
        for item_t, delta, content in grp["items"]:
            words, eff_end, synced = _parse_line_words(
                content, item_t, default_end, offset_sec, delta
            )
            if words:
                parsed_lines.append((words, eff_end, synced))

        if not parsed_lines:
            # Empty timestamp line (instrumental break / end-of-line marker)
            continue

        main_words, main_end, main_synced = parsed_lines[0]
        sub_words = []
        sub_synced = False
        if len(parsed_lines) > 1:
            # Attach second co-timed line as subtitle / romanization
            sub_words, sub_end, sub_synced = parsed_lines[1]
            main_end = max(main_end, sub_end)
            # If 3+ co-timed lines exist, merge remaining text into sub_words
            for extra_words, _, _ in parsed_lines[2:]:
                extra_txt = "".join(w["text"] for w in extra_words).strip()
                if extra_txt and sub_words:
                    sub_words[-1]["text"] += " / " + extra_txt

        # Ensure strictly positive duration
        if main_end <= begin:
            main_end = begin + 0.1

        result.append(
            {
                "begin": begin,
                "end": main_end,
                "words": main_words,
                "synced": main_synced,
                "sub_words": sub_words,
                "sub_synced": sub_synced,
            }
        )

    return meta, result


# ── TTML builder ───────────────────────────────────────────────

def build_ttml(title, lines, lang="ja"):
    ps = []
    for line in lines:
        begin = fmt_ttml(line["begin"])
        end = fmt_ttml(line["end"])
        words = line["words"]
        synced = line.get("synced", len(words) > 1)

        if not synced and len(words) == 1:
            ps.append(f'    <p begin="{begin}" end="{end}">{esc(words[0]["text"])}</p>')
        else:
            spans = "\n".join(
                f'      <span begin="{fmt_ttml(w["begin"])}" end="{fmt_ttml(w["end"])}" xml:space="preserve">{esc(w["text"])}</span>'
                for w in words
            )
            ps.append(f'    <p begin="{begin}" end="{end}">\n{spans}\n    </p>')

    body = "\n".join(ps)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml"
    xmlns:tts="http://www.w3.org/ns/ttml#styling"
    xmlns:ttm="http://www.w3.org/ns/ttml#metadata"
    xml:lang="{esc(lang)}">
  <head>
    <ttm:title>{esc(title)}</ttm:title>
  </head>
  <body>
    <div>
{body}
    </div>
  </body>
</tt>'''


# ── main ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert .lrc (standard or Enhanced LRC) to TTML"
    )
    parser.add_argument("input", help="Input .lrc file")
    parser.add_argument("--output", "-o", help="Output .ttml (default: same stem)")
    parser.add_argument("--title", "-t", help="Song title (skips prompt)")
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Non-interactive (use metadata/filename title)"
    )
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"  error: file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    text = read_text_file(args.input)
    meta, lines = parse_lrc(text)
    interactive = sys.stdin.isatty() and not args.yes

    if args.title is not None:
        title = args.title
    elif "ti" in meta and meta["ti"]:
        title = meta["ti"]
        if interactive:
            print(f'  Title from LRC: "{title}"')
            try:
                override = input("  Press enter to keep, or type a new title: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                override = ""
            if override:
                title = override
    elif interactive:
        try:
            title = input("  Song title: ").strip() or "Untitled"
        except (EOFError, KeyboardInterrupt):
            print()
            title = os.path.splitext(os.path.basename(args.input))[0] or "Untitled"
    else:
        title = os.path.splitext(os.path.basename(args.input))[0] or "Untitled"

    out_path = args.output or os.path.splitext(args.input)[0] + ".ttml"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_ttml(title, lines))

    kind_str = "Enhanced LRC" if any(l.get("synced") for l in lines) else "LRC"
    print(f"  {len(lines)} lines ({kind_str}) → {out_path}")

    # If co-timed secondary lines exist (e.g. bilingual/romaji LRC), note or export
    sub_count = sum(1 for l in lines if l.get("sub_words"))
    if sub_count:
        sub_out = os.path.splitext(out_path)[0] + "_sub.ttml"
        sub_lines = [
            {
                "begin": l["begin"],
                "end": l["end"],
                "words": l["sub_words"],
                "synced": l.get("sub_synced", False),
            }
            for l in lines
            if l.get("sub_words")
        ]
        with open(sub_out, "w", encoding="utf-8") as f:
            f.write(build_ttml(f"{title} (Sub)", sub_lines, lang="ja-Latn"))
        print(f"  {sub_count} co-timed subtitle/romaji lines → {sub_out}")


if __name__ == "__main__":
    main()
