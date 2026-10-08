// Partial batch failure and delayed load isolation, using the loopback QA host.
import { chromium } from '../frontend/node_modules/playwright/index.mjs'
import { readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import assert from 'node:assert/strict'

const root = fileURLToPath(new URL('..', import.meta.url))
const work = path.join(root, '.work')
const port = (await readFile(path.join(work, 'preview-port.txt'), 'utf8')).trim()
const base = `http://127.0.0.1:${port}`
const browser = await chromium.launch({ headless: true,
  ...(process.env.MEDIAARCHIVER_BROWSER ? { executablePath: process.env.MEDIAARCHIVER_BROWSER } : {}) })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const errors = [], steps = []
page.on('pageerror', error => errors.push(error.message))
const ready = () => page.waitForFunction(() => document.querySelector('.ma-canvas>img')?.naturalWidth > 0 && !document.querySelector('.ma-render-pill'))
try {
  await page.goto(base)
  await ready()
  await page.getByRole('button', { name: '预览已选库', exact: true }).waitFor()
  let failedKey, completed = []
  await page.route('**/studio/action', async route => {
    const payload = route.request().postDataJSON()
    if (payload.action !== 'preview') return route.continue()
    if (!failedKey) {
      failedKey = payload.key
      return route.fulfill({ status: 400, contentType: 'application/json',
        body: JSON.stringify({ success: false, message: '本地测试：此库暂时不可用' }) })
    }
    const response = await route.fetch()
    completed.push(payload.key)
    return route.fulfill({ response })
  })
  await page.getByRole('button', { name: '预览已选库', exact: true }).click()
  await page.getByRole('alert').filter({ hasText: '其他已选库已继续处理' }).waitFor({ timeout: 45000 })
  const card = page.locator(`.ma-library-card[data-library-key="${failedKey}"]`)
  assert.equal(await card.locator('.ma-library-art img').count(), 0)
  assert.ok((await card.textContent()).includes('本地测试：此库暂时不可用'))
  assert.equal(completed.length, (await page.locator('.ma-library-card').count()) - 1)
  steps.push('Single-library preview failure is reported and every remaining selected library continues')
  await page.unroute('**/studio/action')
  await page.getByRole('button', { name: '预览已选库', exact: true }).click()
  await page.waitForFunction(() => !document.querySelector('.ma-server-toolbar')?.textContent.includes('逐库预览中'))
  assert.ok((await card.locator('.ma-library-art img').getAttribute('src')).startsWith('data:image/'))
  steps.push('Retry recovers the failed card with its own rendered image')

  let started, release
  const waiting = new Promise(resolve => { started = resolve })
  const hold = new Promise(resolve => { release = resolve })
  await page.route('**/plugin/MediaArchiver/studio', async route => {
    const response = await route.fetch()
    const body = await response.json()
    body.data.cover_servers = [{ id: 'stale-server', name: '过时服务器', gateway: true }]
    started()
    await hold
    await route.fulfill({ response, json: body })
  })
  await page.getByRole('button', { name: '刷新媒体库', exact: true }).click()
  await waiting
  await page.evaluate(() => {
    const select = document.querySelector('#ma-server')
    const option = document.createElement('option')
    option.value = 'new-server'; option.textContent = '新的服务器'
    select.append(option); select.value = option.value
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  release()
  await page.waitForTimeout(500)
  assert.equal(await page.locator('#ma-server').inputValue(), 'new-server')
  steps.push('Delayed server state cannot overwrite a newer server selection')
  assert.deepEqual(errors, [])
  await writeFile(path.join(work, 'ui-reliability-results.json'), JSON.stringify({ steps, errors }, null, 2))
  steps.forEach(step => console.log('PASS:', step))
} finally {
  await browser.close()
}
