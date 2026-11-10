/**
 * 售后核心页面 axe 自动扫描（真实浏览器 + stub/本地后端）。
 *
 * 用法（Windows CMD）：
 *   node frontend\e2e\accessibility.mjs
 *   set APP_URL=http://127.0.0.1:8010/&& set CDP_URL=http://127.0.0.1:9223&& node frontend\e2e\accessibility.mjs
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const APP_URL = process.env.APP_URL || 'http://127.0.0.1:8010/'
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9223'
const A11Y_SCOPE = process.env.A11Y_SCOPE || 'core'
const AXE_SOURCE = readFileSync(
  fileURLToPath(new URL('../node_modules/axe-core/axe.min.js', import.meta.url)),
  'utf8',
)

const results = []
let socket
let sequence = 0

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function record(name, violations) {
  const ok = violations.length === 0
  results.push({ name, ok, violations })
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name} - ${violations.length} 项 WCAG A/AA 违规`)
  for (const violation of violations) {
    console.log(`  ${violation.id} (${violation.impact || 'unknown'}): ${violation.help}`)
    for (const target of violation.targets.slice(0, 5)) console.log(`    - ${target}`)
  }
}

async function freshPageTarget() {
  const created = await fetch(`${CDP_URL}/json/new?${encodeURIComponent(APP_URL)}`, { method: 'PUT' })
  if (!created.ok) throw new Error(`无法创建独立浏览器标签：HTTP ${created.status}`)
  const target = await created.json()
  if (!target?.webSocketDebuggerUrl) throw new Error('新标签缺少 webSocketDebuggerUrl')
  return target.webSocketDebuggerUrl
}

function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url)
    ws.onopen = () => resolve(ws)
    ws.onerror = () => reject(new Error('CDP WebSocket 连接失败'))
  })
}

function send(method, params = {}) {
  const id = ++sequence
  return new Promise((resolve, reject) => {
    const handler = (event) => {
      const payload = JSON.parse(event.data)
      if (payload.id !== id) return
      socket.removeEventListener('message', handler)
      payload.error ? reject(new Error(`${method}: ${JSON.stringify(payload.error)}`)) : resolve(payload.result)
    }
    socket.addEventListener('message', handler)
    socket.send(JSON.stringify({ id, method, params }))
  })
}

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
  return result.result.value
}

async function waitForText(text, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await evaluate(`document.body.innerText.includes(${JSON.stringify(text)})`)) return true
    await sleep(300)
  }
  return false
}

async function navigate(hash, marker) {
  await send('Page.navigate', { url: `${APP_URL}${hash}` })
  if (!await waitForText(marker)) throw new Error(`页面未出现“${marker}”：${hash}`)
}

async function loginAs(username, password) {
  await send('Network.clearBrowserCookies')
  await navigate('#/login', '电商售后工作台')
  const submitted = await evaluate(`(() => {
    const inputs = Array.prototype.slice.call(document.querySelectorAll('input'))
    const usernameInput = inputs.find((item) => item.type === 'text') || inputs[0]
    const passwordInput = inputs.find((item) => item.type === 'password')
    const button = Array.prototype.slice.call(document.querySelectorAll('button'))
      .find((item) => item.innerText.trim() === '登录')
    if (!usernameInput || !passwordInput || !button) return false
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(usernameInput, ${JSON.stringify(username)})
    usernameInput.dispatchEvent(new Event('input', { bubbles: true }))
    setter.call(passwordInput, ${JSON.stringify(password)})
    passwordInput.dispatchEvent(new Event('input', { bubbles: true }))
    button.click()
    return true
  })()`)
  const marker = username === 'reviewer' ? '全部工单' : username === 'admin' ? '工作台' : '我的待办'
  if (!submitted || !await waitForText(marker)) throw new Error(`${username} 登录失败`)
}

async function scan(name) {
  // 稳定扫描时刻，避免 Element Plus 的退场动画把临时节点算进可访问性树。
  await evaluate(`(() => {
    if (document.getElementById('a11y-scan-styles')) return
    const style = document.createElement('style')
    style.id = 'a11y-scan-styles'
    style.textContent = '* { animation: none !important; transition: none !important; }'
    document.head.appendChild(style)
  })()`)
  await evaluate(AXE_SOURCE)
  const violations = await evaluate(`(async () => {
    const report = await axe.run(document, {
      runOnly: {
        type: 'tag',
        values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22a', 'wcag22aa'],
      },
      resultTypes: ['violations'],
    })
    return report.violations.map((item) => ({
      id: item.id,
      impact: item.impact,
      help: item.help,
      targets: item.nodes.map((node) => node.target.join(' ')),
    }))
  })()`)
  record(name, violations)
  if (process.env.A11Y_DEBUG === '1') {
    for (const violation of violations) {
      for (const target of violation.targets.slice(0, 8)) {
        const node = await evaluate(`(() => {
          try {
            const item = document.querySelector(${JSON.stringify(target)})
            return item ? item.outerHTML.slice(0, 500) : null
          } catch { return null }
        })()`)
        if (node) console.log(`      ${node}`)
      }
    }
  }
}

async function scanHistoricalPlatform() {
  await loginAs('admin', 'admin123')
  const pages = [
    ['#/','历史工作台', '工作台'],
    ['#/tasks/new', '历史新建诊断', '新建诊断'],
    ['#/knowledge', '历史知识库管理', '知识库管理'],
    ['#/approvals', '历史人工审核', '人工审核'],
    ['#/metrics', '历史指标查询', '指标查询'],
    ['#/system', '历史系统状态', '系统状态'],
    ['#/evaluations', '历史评测集', '评测集'],
    ['#/policies', '历史访问策略', '访问策略'],
    ['#/audit', '历史审计日志', '审计日志'],
    ['#/qa', '历史知识库问答', '知识库问答'],
  ]
  for (const [hash, name, marker] of pages) {
    await navigate(hash, marker)
    await scan(name)
  }
}

async function main() {
  socket = await connect(await freshPageTarget())
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Page.bringToFront')

  await send('Network.clearBrowserCookies')
  await navigate('#/login', '电商售后工作台')
  await scan('登录页')

  await loginAs('ops', 'ops123')
  await scan('客服工单列表')

  const detailOpened = await evaluate(`(() => {
    const row = document.querySelector('.el-table__body tbody tr, table tbody tr')
    if (!row) return false
    ;(row.querySelector('button[data-ticket-action="open-detail"]') || row).click()
    return true
  })()`)
  if (!detailOpened || !await waitForText('客户反馈')) throw new Error('无法打开客服工单详情')
  await scan('客服工单详情')

  await loginAs('reviewer', 'reviewer123')
  await navigate('#/quality-review', '质检抽查')
  await scan('主管质检页')

  if (A11Y_SCOPE === 'all') await scanHistoricalPlatform()

  const failed = results.filter((item) => !item.ok)
  console.log(`\naxe 扫描：${results.length - failed.length}/${results.length} 个页面状态通过`)
  socket.close()
  process.exit(failed.length ? 1 : 0)
}

main().catch((error) => {
  console.error(`[ERROR] ${error.message}`)
  socket?.close()
  process.exit(2)
})
