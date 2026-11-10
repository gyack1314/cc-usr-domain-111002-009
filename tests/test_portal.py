"""本地门户的协议与浏览器行为检查。"""
import json
import os
import pathlib
import unittest
from urllib.request import Request, urlopen
from playwright.sync_api import sync_playwright, expect
from portal.__main__ import running


class PortalProtocolTests(unittest.TestCase):
    def test_start_and_runtime(self):
        with running() as (base, state):
            with urlopen(base+'/api/runtime') as response:
                self.assertEqual('base', json.load(response)['phase'])

    def test_operator_phase(self):
        with running() as (base, state):
            request=Request(base+'/__control',data=b'{"phase":"changed"}',headers={'Content-Type':'application/json'})
            with urlopen(request) as response:
                self.assertEqual('changed',json.load(response)['phase'])


class BrowserSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver=sync_playwright().start()
        cls.browser=cls.driver.chromium.launch(headless=True,args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.server=running()
        self.base,self.state=self.server.__enter__()
        self.context=self.browser.new_context(accept_downloads=True,viewport={'width':1280,'height':900})
        self.page=self.context.new_page()
        self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.goto(self.base)
        expect(self.page.locator('#status')).not_to_have_text('页面加载中')

    def tearDown(self):
        self.context.close()
        self.server.__exit__(None,None,None)
        self.assertEqual([],self.errors)

    def test_portal_ready(self):
        expect(self.page.locator('h1')).to_be_visible()
        capture=os.environ.get('PORTAL_CAPTURE_DIR')
        if capture:
            target=pathlib.Path(capture)/(pathlib.Path.cwd().name+'.png')
            self.page.screenshot(path=str(target),full_page=True)

    def test_browser_behavior_1(self):
        self.page.set_input_files('#files',[{'name':'report.txt','mimeType':'text/plain','buffer':b'north'},{'name':'report.txt','mimeType':'text/plain','buffer':b'south'}])
        expect(self.page.locator('[data-upload-id]')).to_have_count(2)
        expect(self.page.locator('.check-state').filter(has_text='通过')).to_have_count(2)

    def test_browser_behavior_2(self):
        self.page.select_option('#kind','license')
        expect(self.page.locator('#license-id')).to_be_visible()
        expect(self.page.locator('#report-date')).to_have_count(0)

    def test_browser_behavior_3(self):
        self.page.set_input_files('#files',{'name':'empty.txt','mimeType':'text/plain','buffer':b''})
        expect(self.page.locator('.check-state')).to_have_text('预检失败')
        self.page.click('#preview')
        expect(self.page.locator('#preview-result')).to_contain_text('"attachments": []')
        self.assertFalse(any(a['name']=='publish' for a in self.state['actions']))

    def test_browser_source_edge(self):
        self.page.request.post(self.base+'/__control',data={'phase':'delayed-precheck','version':1})
        self.page.set_input_files('#files',{'name':'hold.txt','mimeType':'text/plain','buffer':b'old'})
        expect(self.page.locator('.check-state')).to_have_text('预检中')
        self.page.locator('[data-remove]').click()
        self.page.set_input_files('#files',{'name':'new.txt','mimeType':'text/plain','buffer':b'new'})
        expect(self.page.locator('.check-state')).to_have_text('通过')
        self.page.request.post(self.base+'/__control',data={'version':2})
        self.page.click('#preview')
        expect(self.page.locator('#preview-result')).to_contain_text('new.txt')
        expect(self.page.locator('#preview-result')).not_to_contain_text('hold.txt')
        expect(self.page.locator('[data-upload-id]')).to_have_count(1)


if __name__ == "__main__":
    unittest.main()
