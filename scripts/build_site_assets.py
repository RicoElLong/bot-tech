"""Create content-addressed CSS/JS so cached files cannot mix site versions.

Run from any directory before committing changes to docs/styles.css or site.js.
Old generated assets are retained to support previously cached HTML.
"""
from hashlib import sha256
from pathlib import Path
import re


def main():
    docs = Path(__file__).resolve().parents[1] / 'docs'
    page_path = docs / 'index.html'
    page = page_path.read_text(encoding='utf-8')
    replacements = []
    for source, pattern in (
        ('styles.css', r'(<link\s+rel="stylesheet"\s+href=")[^"]+("\s*>)'),
        ('site.js', r'(<script\s+src=")[^"]+("\s+defer></script>)'),
    ):
        content = (docs / source).read_bytes()
        digest = sha256(content).hexdigest()[:12]
        stem, ext = source.rsplit('.', 1)
        relative = f'assets/{stem}.{digest}.{ext}'
        updated, count = re.subn(pattern, lambda m: m[1] + relative + m[2], page)
        if count != 1:
            raise SystemExit(f'Expected exactly one reference to {source}, got {count}')
        page = updated
        replacements.append((relative, content))
    for relative, content in replacements:
        (docs / relative).write_bytes(content)
        print(relative)
    page_path.write_text(page, encoding='utf-8')


if __name__ == '__main__':
    main()
