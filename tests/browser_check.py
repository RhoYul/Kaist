import json
from pathlib import Path
from playwright.sync_api import sync_playwright
base=Path(__file__).resolve().parents[1]
cfg=json.loads((base/'config/capture.json').read_text())
assert cfg['demo']
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
    page=browser.new_page(viewport=dict(width=1440,height=1100),device_scale_factor=1)
    errors=[];page.on('pageerror',lambda e: errors.append(str(e)))
    page.goto('http://127.0.0.1:8765',wait_until='networkidle')
    page.locator('#password').fill(cfg['worker_password'])
    page.locator('#login-form button').click()
    page.locator('#app').wait_for(state='visible')
    page.wait_for_timeout(1800)
    page.screenshot(path=str(base/'runtime/worker-screen.png'),full_page=True)
    page.locator('[data-review]').first.click()
    page.locator('#review-dialog').wait_for(state='visible')
    page.locator('#frame-index').fill('30');page.locator('#frame-index').dispatch_event('input')
    page.wait_for_timeout(400)
    page.screenshot(path=str(base/'runtime/review-screen.png'),full_page=True)
    assert page.locator('#qc-results').inner_text()
    page.locator('#close-review').click()
    page.set_viewport_size(dict(width=390,height=844))
    page.screenshot(path=str(base/'runtime/mobile-screen.png'),full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'horizontal overflow'
    adm=browser.new_page(viewport=dict(width=1440,height=1100))
    adm.on('pageerror',lambda e: errors.append(str(e)))
    adm.goto('http://127.0.0.1:8766/admin')
    adm.locator('#password').fill(cfg['admin_password']);adm.locator('#login-form button').click()
    adm.locator('#admin-view').wait_for(state='visible');adm.wait_for_timeout(500)
    adm.screenshot(path=str(base/'runtime/admin-screen.png'),full_page=True)
    assert 'NAS 저장 완료' in adm.locator('#stats').inner_text()
    assert not errors, errors
    print('PASS: worker login, episode review, RGB frame navigation, mobile width, admin dashboard; no browser JS errors')
    browser.close()
