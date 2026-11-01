"""Create content-addressed CSS/JS so cached files cannot mix site versions.

Run from any directory before committing changes to docs/styles.css or site.js.
Old generated assets are retained to support previously cached HTML.
"""
from hashlib import sha256
from pathlib import Path
import re


def main():
    docs = Path(__file__).resolve().parents[1] / 'docs'
    paths = [docs / name for name in ('index.html', 'privacy.html', 'consent.html')]
    pages = {path: path.read_text(encoding='utf-8') for path in paths}
    for source in ('styles.css', 'site.js', 'lead-config.js', 'lead-form.js', 'legal.js'):
        content = (docs / source).read_bytes()
        digest = sha256(content).hexdigest()[:12]
        stem, ext = source.rsplit('.', 1)
        relative = f'assets/{stem}.{digest}.{ext}'
        if ext == 'css':
            pattern = r'(<link\s+rel="stylesheet"\s+href=")[^"]+("\s*>)'
        else:
            version = rf'(?:{re.escape(source)}|assets/{re.escape(stem)}\.[a-f0-9]{{12}}\.js)'
            pattern = rf'(<script\s+src="){version}("\s+defer></script>)'
        total = 0
        for path, page in pages.items():
            pages[path], count = re.subn(pattern, lambda m: m[1] + relative + m[2], page)
            if count > 1:
                raise SystemExit(f'Duplicate reference to {source} in {path.name}')
            total += count
        if not total:
            raise SystemExit(f'No reference to {source}')
        (docs / relative).write_bytes(content)
        print(relative)
    for path, page in pages.items():
        path.write_text(page, encoding='utf-8')


if __name__ == '__main__':
    main()
