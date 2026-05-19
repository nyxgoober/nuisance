#!/usr/bin/env python3

import re
import sys
import os
import argparse

# ── time helpers ───────────────────────────────────────────────

def parse_lrc_ts(ts):
    m = re.match(r'(\d+):(\d+)[\.:](\d+)', ts)
    if not m:
        return None
    minutes = int(m.group(1))
    seconds = int(m.group(2))
    frac    = m.group(3)
    ms = int(frac) * 10 if len(frac) <= 2 else int(frac[:3])
    return minutes * 60 + seconds + ms / 1000

def fmt_ttml(sec):
    h  = int(sec // 3600)
    m  = int((sec % 3600) // 60)
    s  = int(sec % 60)
    ms = round((sec % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

def esc(s):
    return (s.replace('&','&amp;').replace('<','&lt;')
             .replace('>','&gt;').replace('"','&quot;'))

# ── LRC parser ─────────────────────────────────────────────────

LINE_TAG = re.compile(r'\[(\d+:\d+[\.:]\d+)\]')
WORD_TAG = re.compile(r'<(\d+:\d+[\.:]\d+)>')
META_TAG = re.compile(r'^\[([a-zA-Z]+):(.*)\]$')

def parse_lrc(text):
    meta    = {}
    entries = []

    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue

        mm = META_TAG.match(raw)
        if mm:
            meta[mm.group(1).lower()] = mm.group(2).strip()
            continue

        tags = list(LINE_TAG.finditer(raw))
        if not tags:
            continue

        content = raw[tags[-1].end():]

        for tag_match in tags:
            t = parse_lrc_ts(tag_match.group(1))
            if t is not None:
                entries.append((t, content))

    entries.sort(key=lambda x: x[0])

    result = []
    for i, (begin, content) in enumerate(entries):
        end       = entries[i+1][0] if i+1 < len(entries) else begin + 5.0
        word_tags = list(WORD_TAG.finditer(content))

        if word_tags:
            words = []
            for wi, wt in enumerate(word_tags):
                w_begin    = parse_lrc_ts(wt.group(1))
                text_start = wt.end()
                text_end   = word_tags[wi+1].start() if wi+1 < len(word_tags) else len(content)
                text       = content[text_start:text_end]
                w_end      = parse_lrc_ts(word_tags[wi+1].group(1)) if wi+1 < len(word_tags) else end
                if text.strip():
                    words.append({"text": text, "begin": w_begin, "end": w_end})
        else:
            text  = content.strip()
            words = [{"text": text, "begin": begin, "end": end}] if text else []

        if words:
            result.append({"begin": begin, "end": end, "words": words})

    return meta, result

# ── TTML builder ───────────────────────────────────────────────

def build_ttml(title, lines):
    ps = []
    for line in lines:
        begin = fmt_ttml(line["begin"])
        end   = fmt_ttml(line["end"])
        words = line["words"]

        if len(words) == 1:
            ps.append(f'    <p begin="{begin}" end="{end}">{esc(words[0]["text"])}</p>')
        else:
            spans = '\n'.join(
                f'      <span begin="{fmt_ttml(w["begin"])}" end="{fmt_ttml(w["end"])}" xml:space="preserve">{esc(w["text"])}</span>'
                for w in words
            )
            ps.append(f'    <p begin="{begin}" end="{end}">\n{spans}\n    </p>')

    body = '\n'.join(ps)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml"
    xmlns:tts="http://www.w3.org/ns/ttml#styling"
    xmlns:ttm="http://www.w3.org/ns/ttml#metadata"
    xml:lang="ja">
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
    parser = argparse.ArgumentParser(description='Convert .lrc to TTML')
    parser.add_argument('input',           help='Input .lrc file')
    parser.add_argument('--output', '-o',  help='Output .ttml (default: same name)')
    parser.add_argument('--title',  '-t',  help='Song title (skips prompt)')
    args = parser.parse_args()

    with open(args.input, 'r', encoding='utf-8') as f:
        text = f.read()

    meta, lines = parse_lrc(text)

    if args.title:
        title = args.title
    elif 'ti' in meta:
        title = meta['ti']
        print(f'  Title from LRC: "{title}"')
        override = input('  Press enter to keep, or type a new title: ').strip()
        if override:
            title = override
    else:
        title = input('  Song title: ').strip() or 'Untitled'

    out_path = args.output or os.path.splitext(args.input)[0] + '.ttml'

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(build_ttml(title, lines))

    print(f'  {len(lines)} lines → {out_path}')

if __name__ == '__main__':
    main()
