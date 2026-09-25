from __future__ import annotations

import re
import unittest
from importlib import resources


class WebResourceTests(unittest.TestCase):
    def test_built_entrypoints_and_their_dependencies_are_packaged(self) -> None:
        web = resources.files("gpu_watcher.web")
        for page, entry in [("index.html", "app.js"), ("login.html", "login.js")]:
            html = web.joinpath(page).read_text(encoding="utf-8")
            self.assertIn('id="app"', html)
            self.assertIn(f'type="module" src="/static/{entry}"', html)
            self.assertIn('/static/theme.js', html)
            self.assertIn('/static/styles.css', html)
            self.assertIn('/favicon.ico', html)
            source = web.joinpath(entry).read_text(encoding="utf-8")
            imports = re.findall(r'from\s*["\'](\./[^"\']+)["\']', source)
            self.assertTrue(imports, f"{entry} must include its shared Vue runtime")
            for path in imports:
                self.assertTrue(web.joinpath(path.removeprefix('./')).is_file(), path)

        css = web.joinpath('styles.css').read_text(encoding='utf-8')
        fonts = re.findall(r'url\(/static/(vendor/[^)]+\.woff2)\)', css)
        self.assertTrue(fonts, 'Fonts should be self-hosted')
        for font in fonts:
            self.assertEqual(web.joinpath(font).read_bytes()[:4], b'wOF2')
        self.assertEqual(web.joinpath('favicon.ico').read_bytes()[:4], b'\x00\x00\x01\x00')
        self.assertIn('Terminal', web.joinpath('vendor/xterm.js').read_text(encoding='utf-8'))
        self.assertIn('FitAddon', web.joinpath('vendor/addon-fit.js').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
