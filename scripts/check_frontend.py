"""Real Chromium verification of overview, 3D canvas, node/link tabs and APIs."""
import json
import os
from pathlib import Path
import time

LAB = Path(__file__).resolve().parents[2]
EXPECTED_NODES = int(os.environ.get('TE_EXPECTED_NODES', '122'))
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(LAB / '.cache/browsers'))
from playwright.sync_api import sync_playwright

destination = LAB / 'runtime/frontend-check'
destination.mkdir(parents=True, exist_ok=True)
errors = []
responses = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, args=['--no-sandbox', '--use-gl=angle',
                                                              '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
    page = browser.new_page(viewport={'width': 1440, 'height': 1100})
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('response', lambda response: responses.append({'url': response.url, 'status': response.status})
            if response.status >= 400 else None)
    page.goto('http://127.0.0.1:8080/', wait_until='networkidle', timeout=60000)
    page.get_by_text('模拟网络节点数量', exact=True).wait_for()
    page.wait_for_timeout(3000)
    text = page.locator('body').inner_text()
    assert str(EXPECTED_NODES) in text, text[:1000]
    assert '页面渲染失败' not in text
    assert page.locator('canvas').count() > 0, 'missing Cesium canvas'
    page.screenshot(path=str(destination / 'overview.png'), full_page=True)
    page.locator('canvas').first.scroll_into_view_if_needed()
    page.screenshot(path=str(destination / 'scene.png'))
    for label in ('节点', '链路', '概览'):
        page.get_by_role('menuitem', name=label, exact=True).click()
        page.wait_for_timeout(2000)
        assert '页面渲染失败' not in page.locator('body').inner_text()
    page.screenshot(path=str(destination / 'overview-after-navigation.png'), full_page=True)
    report = {'checked_unix': time.time(), 'page_errors': errors, 'http_errors': responses,
              'text': text, 'url': page.url}
    (destination / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    browser.close()
    assert not errors, errors
    assert not responses, responses
