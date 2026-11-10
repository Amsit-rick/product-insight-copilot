/**
 * 售后工作台回归脚本（CDP，零 npm 依赖）。
 *
 * 只验证客服主流程，不运行会产生真实模型费用的分析任务：
 *   ops       -> 我的待办、业务化字段、入口收敛、审批前安全回复
 *   reviewer  -> 人工审核可见，研发诊断入口不可见
 *   admin     -> 诊断平台和运维入口仍可用
 *
 * 用法（Windows CMD）：
 *   node frontend\e2e\after-sales.mjs
 *   set APP_URL=http://127.0.0.1:8010/&& node frontend\e2e\after-sales.mjs
 */

const APP_URL = process.env.APP_URL || 'http://127.0.0.1:8010/'
const CDP_URL = process.env.CDP_URL || 'http://127.0.0.1:9223'
const EXPECT_REFLECTION_UI = process.env.EXPECT_REFLECTION_UI === '1'

const results = []
let socket
let sequence = 0

function record(name, ok, detail = '') {
  results.push({ name, ok })
  console.log(`${ok ? '[PASS]' : '[FAIL]'} ${name}${detail ? ` - ${detail}` : ''}`)
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function targetUrl() {
  try {
    const created = await fetch(`${CDP_URL}/json/new?${encodeURIComponent(APP_URL)}`, { method: 'PUT' })
    if (created.ok) {
      const target = await created.json()
      if (target?.webSocketDebuggerUrl) return target.webSocketDebuggerUrl
    }
  } catch {
    // Older Chromium builds may not expose /json/new; reuse the app page below.
  }
  const pages = await (await fetch(`${CDP_URL}/json/list`)).json()
  const target = pages.find((item) => item.type === 'page' && item.url.startsWith(APP_URL.replace(/\/$/, '')))
  if (!target) throw new Error(`没有找到 ${APP_URL} 对应的浏览器标签`)
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
    try {
      if (await evaluate(`Boolean(document.body && document.body.innerText.includes(${JSON.stringify(text)}))`)) return true
    } catch {
      // 页面刷新/路由切换期间 body 可能暂时不存在，继续轮询而不是让验收脚本退出。
    }
    await sleep(300)
  }
  return false
}

async function waitForVisibleDrawer(timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const visible = await evaluate(`Array.from(document.querySelectorAll('.el-drawer'))
        .some((item) => {
          const style = getComputedStyle(item)
          const overlay = item.closest('.el-overlay')
          const overlayStyle = overlay ? getComputedStyle(overlay) : null
          const hasBusinessHeader = Boolean(item.querySelector('.drawer-head'))
          const hasBackButton = Array.from(item.querySelectorAll('button'))
            .some((button) => button.innerText.includes('返回'))
          return style.visibility !== 'hidden'
            && style.display !== 'none'
            && (!overlayStyle || (overlayStyle.visibility !== 'hidden' && overlayStyle.display !== 'none'))
            && hasBusinessHeader
            && hasBackButton
        })`)
      if (visible) return true
    } catch {
      // 抽屉挂载过渡期间继续等待。
    }
    await sleep(100)
  }
  return false
}

async function waitForHiddenDrawer(timeoutMs = 3000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (!(await waitForVisibleDrawer(100))) return true
    await sleep(100)
  }
  return false
}

async function pageText() {
  return evaluate('document.body?.innerText || ""')
}

async function pressKey(key, code, windowsVirtualKeyCode) {
  await send('Page.bringToFront')
  const text = key === 'Enter' ? '\r' : key === ' ' ? ' ' : ''
  const keyDownParams = {
    key,
    code,
    windowsVirtualKeyCode,
    modifiers: 0,
    location: 0,
  }
  if (text) {
    keyDownParams.text = text
    keyDownParams.unmodifiedText = text
  }
  await send('Input.dispatchKeyEvent', {
    type: text ? 'keyDown' : 'rawKeyDown',
    ...keyDownParams,
  })
  await send('Input.dispatchKeyEvent', {
    type: 'keyUp',
    key,
    code,
    windowsVirtualKeyCode,
    modifiers: 0,
    location: 0,
  })
}

async function accessibleButtonNames() {
  const tree = await send('Accessibility.getFullAXTree')
  return (tree.nodes || [])
    .filter((node) => !node.ignored && node.role?.value === 'button')
    .map((node) => String(node.name?.value || '').trim())
    .filter(Boolean)
}

async function loginAs(username, password) {
  await send('Network.clearBrowserCookies')
  await send('Page.navigate', { url: `${APP_URL}#/login` })
  await waitForText('电商售后工作台')
  await evaluate(`(() => {
    const set = (el, value) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
      setter.call(el, value)
      el.dispatchEvent(new Event('input', { bubbles: true }))
      el.dispatchEvent(new Event('change', { bubbles: true }))
    }
    const inputs = Array.prototype.slice.call(document.querySelectorAll('input'))
    set(inputs.find((item) => item.type === 'text') || inputs[0], ${JSON.stringify(username)})
    set(inputs.find((item) => item.type === 'password'), ${JSON.stringify(password)})
    const loginButton = Array.prototype.slice.call(document.querySelectorAll('button')).find((item) => item.innerText.trim() === '登录')
    if (loginButton) loginButton.click()
  })()`)
  // 侧栏先渲染，业务页再异步挂载；按角色等待真正的首屏标志，避免读取到空壳布局。
  if (username === 'admin') return waitForText('今日成本', 15000)
  if (username === 'reviewer') return waitForText('全部工单', 15000)
  return waitForText('我的待办', 15000)
}

async function logout() {
  await evaluate(`(() => {
    const logoutButton = Array.prototype.slice.call(document.querySelectorAll('button')).find((item) => item.innerText.trim() === '退出')
    if (logoutButton) logoutButton.click()
  })()`)
  await waitForText('电商售后工作台')
}

async function main() {
  socket = await connect(await targetUrl())
  await send('Page.enable')
  await send('Runtime.enable')
  await send('Accessibility.enable')
  await send('Page.bringToFront')
  await send('Emulation.setFocusEmulationEnabled', { enabled: true })

  record('客服账号可登录', await loginAs('ops', 'ops123'))
  const opsText = await pageText()
  record('客服首页使用“我的待办”', /我的待办/.test(opsText) && /点选上方队列可按处理时限筛选/.test(opsText))
  const attentionCardFocused = await evaluate(`(() => {
    const card = Array.prototype.slice.call(document.querySelectorAll('button'))
      .find((item) => item.innerText.includes('等待我回复'))
    if (!card) return false
    card.focus()
    return document.activeElement === card
  })()`)
  if (attentionCardFocused) await pressKey('Enter', 'Enter', 13)
  await sleep(400)
  const attentionQueueText = await pageText()
  const queueStatusIsAnnounced = await evaluate(`Boolean(
    document.querySelector('.queue-filter[role="status"][aria-live="polite"][aria-atomic="true"]')
  )`)
  if (attentionCardFocused) await pressKey('Enter', 'Enter', 13)
  record(
    '客服首页业务处理卡可用键盘打开队列',
    /即将超时/.test(opsText) && /等待我回复/.test(opsText) && /待主管审批/.test(opsText)
      && attentionCardFocused && /当前队列：\s*等待回复/.test(attentionQueueText),
  )
  record('当前行动队列会向读屏软件播报', queueStatusIsAnnounced)
  record('客服侧栏隐藏研发入口', !/诊断工作台|新建诊断|指标查询|系统状态/.test(opsText))
  record('客服侧栏隐藏质检抽查入口', !/质检抽查/.test(opsText))
  record('客服列表显示下一步和处理人', /下一步做什么/.test(opsText) && /处理人/.test(opsText))

  // 用当前客服自己的测试工单验收详情，避免依赖种子数据或其他 E2E 的执行顺序。
  const detailSubject = `客服详情回归-${Date.now()}`
  const detailCustomerId = `E2E-RAW-CUSTOMER-ID-${Date.now()}`
  const detailFixture = await evaluate(`(async () => {
    const csrf = decodeURIComponent((document.cookie.split('; ').find((item) => item.startsWith('pic_csrf=')) || '').slice('pic_csrf='.length))
    const headers = { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' }
    const me = await fetch('/api/v1/auth/me', { credentials: 'same-origin' }).then((response) => response.json())
    const created = await fetch('/api/v1/tickets', {
      method: 'POST', credentials: 'same-origin', headers,
      body: JSON.stringify({
        external_id: 'E2E-DETAIL-' + Date.now(),
        subject: ${JSON.stringify(detailSubject)},
        content: '物流显示已签收，但我没有收到包裹，请帮忙核实。',
        order_id: 'TB202609241806',
        customer_id: ${JSON.stringify(detailCustomerId)},
        channel: 'wecom',
        data_origin: 'test',
      }),
    })
    const ticket = await created.json()
    if (created.status !== 201 || !ticket.id || !me.id) return { created: created.status }
    const assigned = await fetch('/api/v1/tickets/' + ticket.id + '/assign', {
      method: 'POST', credentials: 'same-origin', headers,
      body: JSON.stringify({ assignee_id: me.id, reason: 'E2E：验收客服工单详情' }),
    })
    if (assigned.status !== 200) return { created: created.status, assigned: assigned.status }
    const analyzed = await fetch('/api/v1/tickets/' + ticket.id + '/analyze', {
      method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf },
    })
    for (let index = 0; index < 50; index += 1) {
      const detail = await fetch('/api/v1/tickets/' + ticket.id, { credentials: 'same-origin' }).then((response) => response.json())
      if (detail.recommendation && (detail.analysis?.business_steps || []).length === 7) {
        return {
          created: created.status,
          assigned: assigned.status,
          analyzed: analyzed.status,
          externalId: ticket.external_id,
          customerId: ticket.customer_id,
          businessSteps: detail.analysis.business_steps.length,
          mode: detail.analysis.mode,
          hasSkills: (detail.analysis.applied_skills || []).length > 0,
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 300))
    }
    return {
      created: created.status,
      assigned: assigned.status,
      analyzed: analyzed.status,
      externalId: ticket.external_id,
      businessSteps: 0,
      timeout: true,
    }
  })()`)
  await send('Page.reload', { ignoreCache: true })
  await waitForText('我的待办', 15000)
  const testRecordsEnabled = await evaluate(`(() => {
    const details = document.querySelector('.advanced-filters')
    if (details) details.open = true
    const checkbox = Array.prototype.slice.call(document.querySelectorAll('.el-checkbox'))
      .find((item) => item.innerText.includes('显示测试记录'))
    if (checkbox && !checkbox.classList.contains('is-checked')) checkbox.click()
    return Boolean(checkbox)
  })()`)
  const detailRowReady = testRecordsEnabled && await waitForText(detailSubject, 10000)
  const listAfterCreate = await pageText()
  record(
    '客服列表不暴露原始客户编号',
    detailRowReady && !listAfterCreate.includes(detailFixture?.customerId || detailCustomerId),
  )
  const keyboardDetailReady = detailRowReady && await evaluate(`(() => {
    const row = Array.prototype.slice.call(document.querySelectorAll('.el-table__body tbody tr, table tbody tr'))
      .find((item) => item.innerText.includes(${JSON.stringify(detailSubject)}))
    const button = row && row.querySelector('button[data-ticket-action="open-detail"]')
    if (!button) return false
    button.focus()
    if (document.activeElement !== button
      || !button.getAttribute('aria-label')?.includes(${JSON.stringify(detailSubject)})) return false
    // 验证组件明确绑定的 Enter 契约。浏览器级可信按键路径由独立
    // Chrome DevTools press_key 验收覆盖，避免把本脚本的 CDP 焦点限制误判为产品缺陷。
    button.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter',
      code: 'Enter',
      bubbles: true,
      cancelable: true,
    }))
    return true
  })()`)
  // 这里验证的是“键盘能否打开抽屉”。详情接口是否完整加载由紧接着的独立用例验证，
  // 不把接口延迟或详情字段调整误判成键盘入口失效。
  const keyboardDetailOpened = keyboardDetailReady
    && await waitForVisibleDrawer()
  const keyboardDetailState = keyboardDetailOpened ? '' : await evaluate(`({
    active: document.activeElement?.outerHTML?.slice(0, 180) || '',
    drawer: Array.from(document.querySelectorAll('.el-drawer')).map((item) => ({
      visible: getComputedStyle(item).visibility,
      display: getComputedStyle(item).display,
      text: item.innerText.slice(0, 120),
    })),
    toast: document.querySelector('.toast')?.innerText || '',
  })`)
  record(
    '工单详情提供带名称的键盘入口',
    keyboardDetailOpened,
    `rowReady=${detailRowReady} keyboardReady=${keyboardDetailReady} opened=${keyboardDetailOpened}${keyboardDetailState ? ` state=${JSON.stringify(keyboardDetailState)}` : ''}`,
  )
  if (keyboardDetailOpened) {
    await evaluate(`(() => {
      const back = Array.prototype.slice.call(document.querySelectorAll('button'))
        .find((item) => item.innerText.includes('返回'))
      if (back) back.click()
    })()`)
    await waitForHiddenDrawer()
  }
  const axButtonNames = await accessibleButtonNames()
  record(
    '客服核心按钮在可访问性树中具有业务名称',
    axButtonNames.some((name) => name.includes('查看工单') && name.includes(detailSubject))
      && axButtonNames.some((name) => name.includes('等待我回复')),
  )
  const opened = await evaluate(`(() => {
    const row = Array.prototype.slice.call(document.querySelectorAll('.el-table__body tbody tr, table tbody tr'))
      .find((item) => item.innerText.includes(${JSON.stringify(detailSubject)}))
    if (!row) return false
    ;(row.querySelector('td') || row).click()
    return true
  })()`)
  const detailReopened = opened && await waitForText(detailFixture?.externalId || detailSubject, 20000)
  record(
    '客服可打开工单详情',
    detailFixture?.created === 201 && detailFixture?.assigned === 200
      && detailFixture?.analyzed === 202 && !detailFixture?.timeout
      && testRecordsEnabled && detailReopened,
  )
  const detailText = await pageText()
  record('详情页按四个业务区块组织信息',
    /本单下一步/.test(detailText) && /01\s*客户反馈/.test(detailText)
      && /02\s*已核实的信息/.test(detailText) && /03\s*建议处理/.test(detailText)
      && /04\s*处理记录/.test(detailText),
  )
  record('详情页将工单来源显示为业务名称', /工单来源[\s\S]*企业微信/.test(detailText) && !/工单来源[\s\S]*\bwecom\b/.test(detailText))
  record('详情页显示接手人摘要', /交接摘要/.test(detailText) && /下一步/.test(detailText))
  record('详情页提供内部协作区', /内部协作/.test(detailText) && /添加内部备注/.test(detailText))
  record('详情页提供常用回复模板', /常用回复/.test(detailText) && /等待补充信息/.test(detailText))
  if (EXPECT_REFLECTION_UI) {
    const reflectionState = await evaluate(`(() => {
      const summary = document.querySelector('.reflection-summary')
      const details = summary?.querySelector('details')
      return {
        visible: Boolean(summary && summary.offsetParent !== null),
        text: summary?.innerText || '',
        issuesCollapsed: !details || details.open === false,
      }
    })()`)
    record(
      '工单详情显示反思摘要且问题默认折叠',
      reflectionState.visible
        && /已完成质量复核|需要人工确认/.test(reflectionState.text)
        && reflectionState.issuesCollapsed,
      JSON.stringify(reflectionState),
    )

    const responsiveChecks = []
    for (const width of [375, 768, 1440]) {
      await send('Emulation.setDeviceMetricsOverride', {
        width,
        height: width === 375 ? 812 : 1000,
        deviceScaleFactor: 1,
        mobile: width === 375,
      })
      await sleep(150)
      responsiveChecks.push(await evaluate(`(() => {
        const element = document.querySelector('.reflection-summary')
        if (!element || element.offsetParent === null) return { width: ${width}, visible: false }
        const rect = element.getBoundingClientRect()
        const box = (selector) => {
          const item = document.querySelector(selector)
          if (!item) return null
          const value = item.getBoundingClientRect()
          return { left: Math.round(value.left), right: Math.round(value.right), width: Math.round(value.width) }
        }
        return {
          width: ${width},
          visible: true,
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          viewport: document.documentElement.clientWidth,
          fits: rect.left >= 0 && rect.right <= document.documentElement.clientWidth + 1,
          drawer: box('.el-drawer'),
          drawerBody: box('.el-drawer__body'),
          layout: box('.drawer-body'),
          column: box('.col'),
          card: box('.reflection-summary')?.width,
        }
      })()`))
    }
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1440,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    })
    record(
      '反思摘要适配 375/768/桌面宽度',
      responsiveChecks.every((item) => item.visible && item.fits),
      JSON.stringify(responsiveChecks),
    )
  }
  const collaborationDefault = await evaluate(`(() => {
    const disclosure = document.querySelector('.business-collaboration__details')
    return {
      exists: Boolean(disclosure),
      open: disclosure?.open ?? true,
      summary: disclosure?.querySelector('summary')?.innerText || '',
    }
  })()`)
  record(
    '多角色协作默认只显示简明摘要',
    detailFixture?.businessSteps === 7 && detailFixture?.mode === 'multi_agent'
      && /本次由 7 个业务角色协作/.test(detailText)
      && /问题分类/.test(detailText) && collaborationDefault.exists
      && !collaborationDefault.open && /展开查看各角色做了什么/.test(collaborationDefault.summary),
  )
  await evaluate(`(() => {
    const disclosure = document.querySelector('.business-collaboration__details')
    if (disclosure) disclosure.open = true
  })()`)
  const expandedCollaborationText = await pageText()
  record(
    '展开后可核对真实多角色分工与原因',
    detailFixture?.businessSteps === 7 && detailFixture?.mode === 'multi_agent'
      && /问题分类/.test(expandedCollaborationText)
      && /订单与政策核验（并行）/.test(expandedCollaborationText)
      && /风险复核/.test(expandedCollaborationText)
      && /需要多角色核对事实、规则和处理风险/.test(expandedCollaborationText),
  )
  record('展开协作详情后显示本次核验重点', detailFixture?.hasSkills === true && /本次核验重点/.test(expandedCollaborationText))
  await evaluate(`(() => {
    const disclosure = document.querySelector('.business-collaboration__details')
    if (disclosure) disclosure.open = false
  })()`)
  record('客服工作台提供待我关注入口', /待我关注/.test(opsText) && !/管理回复模板/.test(opsText))
  record('客服首屏不展示置信度小数', !/置信度\s*0\.\d+/.test(detailText))
  record(
    '客服详情完全隐藏技术诊断信息',
    !/系统核验过程|技术记录|Trace ID|\btokens\b|工作流：after_sales|政策库现状|ticket_created|ready_to_reply/.test(expandedCollaborationText),
  )

  await evaluate("location.hash = '#/system'")
  await waitForText('我的待办', 15000)
  const opsDirectRoute = await evaluate('location.hash')
  record('客服手动输入技术页面地址仍被送回工单', /#\/tickets/.test(opsDirectRoute))

  // 独立创建一张待审批退款单，避免依赖列表第一行或前一套 E2E 遗留数据。
  const approvalSubject = `审批前安全回复回归-${Date.now()}`
  const approvalTicket = await evaluate(`(async () => {
    const csrf = decodeURIComponent((document.cookie.split('; ').find((item) => item.startsWith('pic_csrf=')) || '').slice('pic_csrf='.length))
    const headers = { 'X-CSRF-Token': csrf, 'Content-Type': 'application/json' }
    const created = await fetch('/api/v1/tickets', {
      method: 'POST', credentials: 'same-origin', headers,
      body: JSON.stringify({
        external_id: 'E2E-APPROVAL-' + Date.now(),
        subject: ${JSON.stringify(approvalSubject)},
        content: '商品不想要了，申请退款，请把钱退给我。',
        order_id: 'TB202609241501',
        data_origin: 'test',
      }),
    })
    const ticket = await created.json()
    if (created.status !== 201) return { status: created.status }
    const me = await fetch('/api/v1/auth/me', { credentials: 'same-origin' }).then((response) => response.json())
    const assigned = await fetch('/api/v1/tickets/' + ticket.id + '/assign', {
      method: 'POST', credentials: 'same-origin', headers,
      body: JSON.stringify({ assignee_id: me.id, reason: 'E2E：验证我的待审批工单' }),
    })
    if (assigned.status !== 200) return { status: created.status, assigned: assigned.status }
    await fetch('/api/v1/tickets/' + ticket.id + '/analyze', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrf } })
    for (let index = 0; index < 50; index += 1) {
      const detail = await fetch('/api/v1/tickets/' + ticket.id, { credentials: 'same-origin' }).then((response) => response.json())
      if (detail.recommendation?.status === 'pending_approval') return { status: created.status, assigned: assigned.status, id: ticket.id }
      await new Promise((resolve) => setTimeout(resolve, 300))
    }
    return { status: created.status, assigned: assigned.status, id: ticket.id, timeout: true }
  })()`)
  await send('Page.reload', { ignoreCache: true })
  await waitForText('我的待办', 15000)
  await evaluate(`(() => {
    const details = document.querySelector('.advanced-filters')
    if (details) details.open = true
    const checkbox = Array.prototype.slice.call(document.querySelectorAll('.el-checkbox'))
      .find((item) => item.innerText.includes('显示测试记录'))
    if (checkbox && !checkbox.classList.contains('is-checked')) checkbox.click()
    return Boolean(checkbox)
  })()`)
  await evaluate(`(() => {
    const input = document.querySelector('input[aria-label="搜索工单"]')
    if (!input) return false
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(input, ${JSON.stringify(approvalSubject)})
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
    input.focus()
    return true
  })()`)
  await pressKey('Enter', 'Enter', 13)
  await sleep(600)
  const approvalOpened = await evaluate(`(() => {
    const row = Array.prototype.slice.call(document.querySelectorAll('.el-table__body tbody tr, table tbody tr'))
      .find((item) => item.innerText.includes(${JSON.stringify(approvalSubject)}))
    if (!row) return false
    ;(row.querySelector('td') || row).click()
    return true
  })()`)
  const approvalDetailLoaded = approvalOpened && await waitForText('需要人工决定', 10000)
  const approvalReply = await evaluate(`(() => { const textarea = document.querySelector('textarea'); return textarea ? textarea.value : '' })()`)
  record(
    '审批前回复不作确定性承诺',
    approvalTicket?.status === 201 && approvalTicket?.assigned === 200 && !approvalTicket?.timeout && approvalDetailLoaded
      && !/将为您发起退款|已为您发起退款|预计\s*\d+\s*[-到至]\s*\d+\s*个工作日到账/.test(approvalReply),
  )
  if (EXPECT_REFLECTION_UI) {
    const approvalReflection = await evaluate(`(() => {
      const summary = document.querySelector('.reflection-summary')
      const details = summary?.querySelector('details')
      return {
        visible: Boolean(summary && summary.offsetParent !== null),
        text: summary?.innerText || '',
        revised: /已完成质量复核并修正\\s*\\d+\\s*项/.test(summary?.innerText || ''),
        issuesCollapsed: !details || details.open === false,
      }
    })()`)
    record(
      '待审批退款在浏览器显示反思修订结果',
      approvalReflection.visible && approvalReflection.revised && approvalReflection.issuesCollapsed,
      JSON.stringify(approvalReflection),
    )
  }

  // 缺订单号不能继续弹一个无意义的“确认分析”，必须直接给出业务下一步。
  const missingOrderExternalId = `E2E-NO-ORDER-${Date.now()}`
  const missingOrderSubject = `缺订单号补录入口回归-${Date.now()}`
  const missingOrderContent = `我的包裹还没有收到，但现在找不到订单号。${missingOrderExternalId}`
  const missingOrderCreated = await evaluate(`(async () => {
    const response = await fetch('/api/v1/tickets', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'X-CSRF-Token': decodeURIComponent((document.cookie.split('; ').find((item) => item.startsWith('pic_csrf=')) || '').slice('pic_csrf='.length)),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        external_id: ${JSON.stringify(missingOrderExternalId)},
        subject: ${JSON.stringify(missingOrderSubject)},
        content: ${JSON.stringify(missingOrderContent)},
        data_origin: 'test',
      }),
    })
    const ticket = await response.json()
    if (response.status !== 201) return { status: response.status }
    const me = await fetch('/api/v1/auth/me', { credentials: 'same-origin' }).then((result) => result.json())
    const assigned = await fetch('/api/v1/tickets/' + ticket.id + '/assign', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'X-CSRF-Token': decodeURIComponent((document.cookie.split('; ').find((item) => item.startsWith('pic_csrf=')) || '').slice('pic_csrf='.length)),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ assignee_id: me.id, reason: 'E2E：验证我的缺订单号工单' }),
    })
    return { status: response.status, assigned: assigned.status }
  })()`)
  await send('Page.reload', { ignoreCache: true })
  await waitForText('我的待办', 15000)
  const defaultListText = await pageText()
  record(
    '客服默认待办不混入自动化测试工单',
    missingOrderCreated?.status === 201 && missingOrderCreated?.assigned === 200
      && !defaultListText.includes('缺订单号补录入口回归'),
  )
  await evaluate(`(() => {
    const details = document.querySelector('.advanced-filters')
    if (details) details.open = true
    const checkbox = Array.prototype.slice.call(document.querySelectorAll('.el-checkbox'))
      .find((item) => item.innerText.includes('显示测试记录'))
    if (checkbox && !checkbox.classList.contains('is-checked')) checkbox.click()
    return Boolean(checkbox)
  })()`)
  await evaluate(`(() => {
    const input = document.querySelector('input[aria-label="搜索工单"]')
    if (!input) return false
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(input, ${JSON.stringify(missingOrderSubject)})
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
    input.focus()
    return true
  })()`)
  await pressKey('Enter', 'Enter', 13)
  await sleep(600)
  const missingOrderOpened = await evaluate(`(() => {
    const row = Array.prototype.slice.call(document.querySelectorAll('.el-table__body tbody tr, table tbody tr'))
      .find((item) => item.innerText.includes(${JSON.stringify(missingOrderSubject)}))
    if (!row) return false
    ;(row.querySelector('td') || row).click()
    return true
  })()`)
  // 表格只显示 external_id 的末 8 位，不能用完整 external_id 定位；
  // 等待仅在详情抽屉出现的客户原话，确认点击和详情异步加载都已完成。
  const missingOrderDetailLoaded = missingOrderOpened && await waitForText(missingOrderExternalId, 10000)
  const missingOrderButtons = await evaluate(`Array.from(document.querySelectorAll('button'))
    .map((item) => item.innerText.trim()).filter(Boolean)`)
  const analyzeWithoutOrderClicked = missingOrderDetailLoaded && await evaluate(`(() => {
    const button = Array.prototype.slice.call(document.querySelectorAll('button'))
      .find((item) => item.innerText.trim() === '启动分析')
    if (!button) return false
    button.click()
    return true
  })()`)
  const missingOrderDialog = analyzeWithoutOrderClicked && await waitForText('先补齐订单号', 10000)
  const missingOrderText = await pageText()
  record(
    '缺订单号时分析前给出三个可执行下一步',
    missingOrderCreated?.status === 201 && missingOrderCreated?.assigned === 200
      && missingOrderDetailLoaded && missingOrderDialog
      && /补录并分析/.test(missingOrderText) && /向客户索取/.test(missingOrderText) && /转人工/.test(missingOrderText),
    JSON.stringify({
      created: missingOrderCreated,
      detailLoaded: missingOrderDetailLoaded,
      analyzeClicked: analyzeWithoutOrderClicked,
      buttons: missingOrderButtons,
      dialogVisible: missingOrderDialog,
      hasCompleteAndAnalyze: /补录并分析/.test(missingOrderText),
      hasAskCustomer: /向客户索取/.test(missingOrderText),
      hasManualHandoff: /转人工/.test(missingOrderText),
    }),
  )
  const orderSavedAndStarted = await evaluate(`(() => {
    const dialog = Array.prototype.slice.call(document.querySelectorAll('.el-dialog'))
      .find((item) => item.innerText.includes('先补齐订单号'))
    const input = dialog && dialog.querySelector('input')
    const button = dialog && Array.prototype.slice.call(dialog.querySelectorAll('button'))
      .find((item) => item.innerText.trim() === '补录并分析')
    if (!input || !button) return false
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(input, 'TB202609241806')
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
    button.click()
    return true
  })()`)
  const orderStartedToast = orderSavedAndStarted && await waitForText('已启动分析', 15000)
  const toastIsAnnounced = orderStartedToast && await evaluate(`Boolean(
    document.querySelector('.toast[role="status"][aria-live="polite"][aria-atomic="true"]')
  )`)
  record('补录订单号后可直接启动分析', orderStartedToast)
  record('操作结果会向读屏软件播报', toastIsAnnounced)

  await logout()
  record('主管账号可登录', await loginAs('reviewer', 'reviewer123'))
  const reviewerText = await pageText()
  record('主管可见人工审核入口', /人工审核/.test(reviewerText))
  record('主管可见回复模板维护入口', /管理回复模板/.test(reviewerText))
  record('主管可见待我关注入口', /待我关注/.test(reviewerText))
  record('主管可见质检抽查入口', /质检抽查/.test(reviewerText))
  record('主管默认不进入研发诊断平台', !/诊断工作台|新建诊断|指标查询|系统状态/.test(reviewerText))

  await evaluate("location.hash = '#/quality-review'")
  record('主管可打开质检抽查页', await waitForText('质检抽查', 15000))
  await waitForText('本次抽查结论', 15000)
  const qualityText = await pageText()
  record('质检页说明结果不会发送给客户', /不会发送给客户/.test(qualityText) && /抽查结论/.test(qualityText))
  const rawIssueCodes = qualityText.match(/\b(?:logistics_stalled|missing_item|not_shipped|promotion_not_applied|description_mismatch)\b/g) || []
  const hasChineseIssueLabel = /退款申请|退货申请|换货申请|物流停滞|商品少件|商品破损|错发商品|整单未发货|优惠未生效|商品与描述不符|其他问题/.test(qualityText)
  const hasNoQualityRows = /暂无数据|暂无可抽查工单/.test(qualityText)
  const rawIssueLocations = rawIssueCodes.length ? await evaluate(`Array.from(document.querySelectorAll('body *'))
    .filter((element) => element.children.length === 0 && /\\b(?:logistics_stalled|missing_item|not_shipped|promotion_not_applied|description_mismatch)\\b/.test(element.textContent || ''))
    .map((element) => ({ tag: element.tagName, className: String(element.className || ''), text: element.textContent.trim().slice(0, 180) }))
    .slice(0, 8)`) : []
  record(
    '质检列表和详情使用中文业务标签',
    /优先级/.test(qualityText) && /客户问题/.test(qualityText) && /系统建议/.test(qualityText)
      && (hasChineseIssueLabel || hasNoQualityRows) && rawIssueCodes.length === 0,
    rawIssueCodes.length
      ? `原始枚举仍出现：${[...new Set(rawIssueCodes)].join(', ')}；位置=${JSON.stringify(rawIssueLocations)}`
      : `未发现英文枚举；中文问题标签=${hasChineseIssueLabel}；空列表=${hasNoQualityRows}`,
  )
  const qualitySubmitted = await evaluate(`(() => {
    const textarea = document.querySelector('textarea')
    const save = Array.prototype.slice.call(document.querySelectorAll('button')).find((item) => item.innerText.trim() === '保存质检结果')
    if (!textarea || !save) return false
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set
    setter.call(textarea, 'E2E 质检：已核对事实和回复门禁。')
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    textarea.dispatchEvent(new Event('change', { bubbles: true }))
    save.click()
    return true
  })()`)
  record('主管可提交客户不可见的质检结果', qualitySubmitted && await waitForText('质检结果已记录', 10000))

  await logout()
  record('管理员账号可登录', await loginAs('admin', 'admin123'))
  const adminText = await pageText()
  record('管理员保留完整管理入口', /诊断工作台/.test(adminText) && /系统状态/.test(adminText) && /审计日志/.test(adminText) && /质检抽查/.test(adminText))

  const failed = results.filter((item) => !item.ok)
  console.log(`\n售后回归：${results.length - failed.length}/${results.length} 通过`)
  if (failed.length) console.log(`失败项：${failed.map((item) => item.name).join('、')}`)
  socket.close()
  process.exit(failed.length ? 1 : 0)
}

main().catch((error) => {
  console.error(`[ERROR] ${error.message}`)
  socket?.close()
  process.exit(2)
})
