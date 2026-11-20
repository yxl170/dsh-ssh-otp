/**
 * dsh-ssh-otp —— 带二次验证（TOTP）的 SSH 桥接插件（Host 半侧）。
 *
 * 设计要点：
 * - 工具注册走 ctx.tools.register(defineTool(...))；
 * - 密钥只在调用瞬间从宿主凭据库解析，通过 stdin 管道交给常驻的 Python 子进程，
 *   不写盘、不进日志、不进配置；
 * - SSH 由子进程里的 paramiko 负责（走 keyboard-interactive，能应答动态验证码），
 *   Node 侧只做协议编排、凭据解析与输出整形。
 */
import { defineTool } from '@deepseek-ai/dsh-tools'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'ssh-otp'
export const inject = ['tools', 'webServer']

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))
const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh')
const CONFIG_FILE = join(DSH_HOME, 'dsh-ssh-otp.json')
const SPILL_DIR = join(DSH_HOME, 'ssh-otp-spill')

/** 单流内联上限；超出则落盘并只回头部 */
const INLINE_MAX_BYTES = 64 * 1024
const INLINE_HEAD_CHARS = 32 * 1024
const DEFAULT_TIMEOUT_MS = 120000

const DEFAULTS = {
  pythonPath: join(DSH_HOME, 'runtimes', 'dsh-ssh-otp', 'Scripts', 'python.exe'),
  servicePath: join(PLUGIN_DIR, 'server', 'ssh_service.py'),
  idleMinutes: 10,
  timeoutMs: DEFAULT_TIMEOUT_MS,
  hosts: {},
}

function loadConfig(logger) {
  if (!existsSync(CONFIG_FILE)) {
    return { ...DEFAULTS, hosts: {} }
  }
  try {
    const parsed = JSON.parse(readFileSync(CONFIG_FILE, 'utf8'))
    return { ...DEFAULTS, ...parsed, hosts: parsed.hosts ?? {} }
  } catch (error) {
    logger?.warn?.(`ssh-otp: 配置文件解析失败，按空配置处理：${String(error?.message ?? error)}`)
    return { ...DEFAULTS, hosts: {} }
  }
}

/** 输出整形：小结果内联，大结果落盘并明确告知截断事实 */
function shapeStream(text, label) {
  const bytes = Buffer.byteLength(text ?? '', 'utf8')
  if (bytes <= INLINE_MAX_BYTES) {
    return { text: text ?? '', truncated: false, bytes }
  }
  mkdirSync(SPILL_DIR, { recursive: true })
  const file = join(SPILL_DIR, `${label}-${Date.now()}.log`)
  writeFileSync(file, text, 'utf8')
  const head = (text ?? '').slice(0, INLINE_HEAD_CHARS)
  return {
    text: `${head}\n…[output truncated: ${bytes} bytes total, full output at ${file}]`,
    truncated: true,
    bytes,
    spillPath: file,
  }
}

/** 常驻 Python 服务的 JSON-lines 客户端 */
class SshBridge {
  constructor(getConfig, logger) {
    this.getConfig = getConfig
    this.logger = logger
    this.child = null
    this.pending = new Map()
    this.nextId = 1
    this.buffer = ''
  }

  ensureStarted() {
    if (this.child && this.child.exitCode === null) return
    const config = this.getConfig()
    if (!existsSync(config.pythonPath)) {
      throw new Error(`找不到 Python 解释器：${config.pythonPath}（请检查 ${CONFIG_FILE} 的 pythonPath）`)
    }
    if (!existsSync(config.servicePath)) {
      throw new Error(`找不到服务脚本：${config.servicePath}`)
    }
    const child = spawn(config.pythonPath, [config.servicePath], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
      windowsHide: true,
    })
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => this.#onData(chunk))
    child.stderr.on('data', (chunk) => {
      for (const line of String(chunk).split('\n')) {
        if (line.trim() !== '') this.logger?.info?.(`ssh-otp/svc: ${line.trim()}`)
      }
    })
    child.on('exit', (code) => {
      this.logger?.info?.(`ssh-otp: 服务进程已退出（code=${code}）`)
      for (const [, entry] of this.pending) entry.reject(new Error('SSH 服务进程已退出'))
      this.pending.clear()
      this.child = null
    })
    this.child = child
  }

  #onData(chunk) {
    this.buffer += chunk
    let index = this.buffer.indexOf('\n')
    while (index >= 0) {
      const line = this.buffer.slice(0, index).trim()
      this.buffer = this.buffer.slice(index + 1)
      if (line !== '') this.#onLine(line)
      index = this.buffer.indexOf('\n')
    }
  }

  #onLine(line) {
    let message
    try {
      message = JSON.parse(line)
    } catch {
      this.logger?.warn?.(`ssh-otp: 服务返回了非 JSON 行：${line.slice(0, 200)}`)
      return
    }
    const entry = this.pending.get(message.id)
    if (!entry) return
    this.pending.delete(message.id)
    if (message.ok) entry.resolve(message)
    else {
      const error = new Error(message.error ?? 'SSH 服务返回失败')
      error.code = message.code
      entry.reject(error)
    }
  }

  request(payload) {
    this.ensureStarted()
    const id = this.nextId++
    const body = JSON.stringify({ ...payload, id })
    return new Promise((resolvePromise, rejectPromise) => {
      this.pending.set(id, { resolve: resolvePromise, reject: rejectPromise })
      this.child.stdin.write(body + '\n', 'utf8', (error) => {
        if (error) {
          this.pending.delete(id)
          rejectPromise(error)
        }
      })
    })
  }

  dispose() {
    if (!this.child) return
    try {
      this.child.stdin.end()
      this.child.kill()
    } catch {
      /* 已退出 */
    }
    this.child = null
  }
}

export function apply(ctx, _config) {
  const logger = ctx.logger
  const bridge = new SshBridge(() => loadConfig(logger), logger)

  ctx.effect(() => () => bridge.dispose(), 'ssh-otp: bridge')

  /** 解析某台主机本次调用所需的凭据（每次调用都重新解析，遵循凭据契约） */
  async function resolveConn(alias) {
    const config = loadConfig(logger)
    const host = config.hosts?.[alias]
    if (!host) {
      const known = Object.keys(config.hosts ?? {})
      throw new Error(`未配置主机 "${alias}"。${known.length ? `已配置：${known.join(', ')}` : `请先编辑 ${CONFIG_FILE} 添加主机`}`)
    }
    const credentials = ctx.get('credentials')
    if (credentials === undefined) {
      throw new Error('凭据服务（credentials）未挂载，无法解析 SSH 密钥；请检查 DSH profile 的 credentials 插件。')
    }
    const auth = host.auth ?? {}
    const conn = { host: host.host, port: host.port ?? 22, user: host.user, auth: { kind: auth.kind ?? 'password' } }
    const need = (name, ref) => {
      if (!ref) throw new Error(`主机 "${alias}" 未配置 ${name} 的凭据引用名（如 ${name}Ref）`)
      return credentialRef(ref)
    }
    const passwordRef = need('password', auth.passwordRef)
    const resolved = await credentials.resolve(passwordRef)
    if (resolved === undefined || resolved.value === '') {
      throw new Error(`凭据 "${auth.passwordRef}" 未设置。请在设置页填写，或往 ${join(DSH_HOME, '.credentials.yaml')} 的 refs 里加一条（值要加引号）。`)
    }
    conn.auth.password = resolved.value
    if ((auth.kind ?? 'password') === 'password_totp') {
      const totpRef = need('totp', auth.totpRef)
      const seed = await credentials.resolve(totpRef)
      if (seed === undefined || seed.value === '') {
        throw new Error(`凭据 "${auth.totpRef}"（TOTP 种子）未设置。`)
      }
      conn.auth.totp_seed = seed.value
    }
    return { conn, timeoutMs: config.timeoutMs ?? DEFAULT_TIMEOUT_MS, host }
  }

  function resolveLocalPath(input) {
    if (isAbsolute(input)) return input
    return resolve(process.cwd(), input)
  }

  /** 主机清单 + 凭据就绪状态（工具与设置页路由共用） */
  async function describeHosts() {
    const config = loadConfig(logger)
    const credentials = ctx.get('credentials')
    const cache = new Map()
    const check = async (ref) => {
      if (!ref || !credentials) return false
      if (!cache.has(ref)) {
        let ready = false
        try {
          const value = await credentials.resolve(credentialRef(ref))
          ready = value !== undefined && value.value !== ''
        } catch {
          ready = false
        }
        cache.set(ref, ready)
      }
      return cache.get(ref)
    }
    const rows = []
    for (const [alias, host] of Object.entries(config.hosts ?? {})) {
      const auth = host.auth ?? {}
      rows.push({
        alias,
        target: `${host.user}@${host.host}:${host.port ?? 22}`,
        host: host.host,
        port: host.port ?? 22,
        user: host.user,
        authKind: auth.kind ?? 'password',
        passwordRef: auth.passwordRef ?? null,
        totpRef: auth.totpRef ?? null,
        passwordReady: await check(auth.passwordRef),
        totpReady: (auth.kind ?? 'password') === 'password_totp' ? await check(auth.totpRef) : true,
      })
    }
    return rows
  }

  // ---------------------------------------------------------------- 设置页路由
  function isLoopback(req) {
    const address = req.socket?.remoteAddress ?? ''
    const host = String(req.headers?.host ?? '')
    const addressOk = address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1'
    const hostOk = /^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/i.test(host)
    return addressOk && hostOk
  }

  function sendJson(res, status, body) {
    res.statusCode = status
    res.setHeader('content-type', 'application/json; charset=utf-8')
    res.end(JSON.stringify(body))
  }

  async function readJsonBody(req, limit = 64 * 1024) {
    const chunks = []
    let size = 0
    for await (const chunk of req) {
      size += chunk.length
      if (size > limit) throw new Error('请求体过大')
      chunks.push(chunk)
    }
    if (chunks.length === 0) return null
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  }

  function registerRoutes() {
    const webServer = ctx.get('webServer')
    if (webServer === undefined) {
      logger?.warn?.('ssh-otp: webServer 未挂载，跳过设置页路由（工具仍可用）')
      return () => {}
    }

    const knownRefs = () => {
      const config = loadConfig(logger)
      const refs = new Set()
      for (const host of Object.values(config.hosts ?? {})) {
        const auth = host.auth ?? {}
        if (auth.passwordRef) refs.add(auth.passwordRef)
        if (auth.totpRef) refs.add(auth.totpRef)
      }
      return refs
    }

    const routes = [
      {
        kind: 'exact',
        path: '/plugins/dsh-ssh-otp/state',
        handler: async (req, res) => {
          if (!isLoopback(req)) return sendJson(res, 403, { error: 'forbidden: loopback-only' })
          if (req.method !== 'GET') return sendJson(res, 405, { error: 'method not allowed' })
          try {
            const credentials = ctx.get('credentials')
            const hosts = await describeHosts()
            let refStates = []
            if (credentials !== undefined) {
              // 宿主侧 describe 只接受单个 ref（批量形式是浏览器侧的 Remote 接口）
              for (const ref of knownRefs()) {
                try {
                  const info = await credentials.describe(credentialRef(ref))
                  refStates.push({ ref, configured: info?.configured === true, source: info?.source ?? null })
                } catch (error) {
                  refStates.push({ ref, configured: false, error: String(error?.message ?? error) })
                }
              }
            }
            sendJson(res, 200, { hosts, refs: refStates, credentialsAvailable: credentials !== undefined })
          } catch (error) {
            sendJson(res, 500, { error: String(error?.message ?? error) })
          }
        },
      },
      {
        kind: 'exact',
        path: '/plugins/dsh-ssh-otp/secret',
        handler: async (req, res) => {
          if (!isLoopback(req)) return sendJson(res, 403, { error: 'forbidden: loopback-only' })
          if (req.method !== 'POST') return sendJson(res, 405, { error: 'method not allowed' })
          try {
            const body = await readJsonBody(req)
            const ref = String(body?.ref ?? '')
            const value = String(body?.value ?? '')
            if (ref === '' || value === '') return sendJson(res, 400, { error: 'ref 与 value 都不能为空' })
            if (!knownRefs().has(ref)) return sendJson(res, 400, { error: `未在本插件配置中出现的凭据名：${ref}` })
            const credentials = ctx.get('credentials')
            if (credentials === undefined) return sendJson(res, 503, { error: '凭据服务未挂载' })
            credentialRef(ref)
            await credentials.set(credentialRef(ref), value)
            sendJson(res, 200, { ok: true, ref })
          } catch (error) {
            sendJson(res, 400, { error: String(error?.message ?? error) })
          }
        },
      },
      {
        kind: 'exact',
        path: '/plugins/dsh-ssh-otp/test',
        handler: async (req, res) => {
          if (!isLoopback(req)) return sendJson(res, 403, { error: 'forbidden: loopback-only' })
          if (req.method !== 'POST') return sendJson(res, 405, { error: 'method not allowed' })
          try {
            const body = await readJsonBody(req)
            const alias = String(body?.alias ?? '')
            if (alias === '') return sendJson(res, 400, { error: '缺少 alias' })
            const { conn } = await resolveConn(alias)
            const payload = await bridge.request({
              op: 'exec',
              alias,
              conn,
              command: 'hostname; whoami',
              timeoutMs: 60000,
            })
            sendJson(res, 200, {
              ok: payload.exitCode === 0,
              exitCode: payload.exitCode,
              output: String(payload.stdout ?? '').trim(),
              host: alias,
              checkedAt: Date.now(),
            })
          } catch (error) {
            sendJson(res, 200, { ok: false, error: String(error?.message ?? error) })
          }
        },
      },
    ]

    return ctx.effect(() => {
      const disposers = routes.map((route) => webServer.register(route))
      return () => {
        for (const dispose of disposers) dispose()
      }
    }, 'ssh-otp: routes')
  }

  registerRoutes()

  const tools = []

  tools.push(defineTool({
    name: 'ssh_otp_hosts',
    description: '列出本插件配置的 SSH 主机，以及它们的密码/二次验证凭据是否已在宿主凭据库中就位。',
    parameters: {},
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          hosts: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                alias: { type: 'string', required: true },
                target: { type: 'string', required: true },
                authKind: { type: 'string', required: true },
                passwordReady: { type: 'boolean', required: true },
                totpReady: { type: 'boolean', required: true },
              },
            },
          },
        },
      },
      render: (_args, value) => {
        const lines = value.hosts.map((h) => {
          const marks = [h.passwordReady ? '密码✓' : '密码✗']
          if (h.authKind === 'password_totp') marks.push(h.totpReady ? 'TOTP✓' : 'TOTP✗')
          return `${h.alias}  ${h.target}  [${h.authKind}]  ${marks.join(' ')}`
        })
        return [{ type: 'text', text: lines.length ? lines.join('\n') : '（尚未配置任何主机）' }]
      },
    },
    isConcurrencySafe: () => true,
    async execute() {
      const rows = await describeHosts()
      return {
        hosts: rows.map((row) => ({
          alias: row.alias,
          target: row.target,
          authKind: row.authKind,
          passwordReady: row.passwordReady,
          totpReady: row.totpReady,
        })),
      }
    },
  }))

  tools.push(defineTool({
    name: 'ssh_otp_exec',
    description: '在远程主机上执行一条 shell 命令并返回 exit code、stdout、stderr。认证用密码 + 本地生成的 TOTP 验证码（适用于开启二次验证、且禁用了公钥认证的集群）。',
    parameters: {
      host: { type: 'string', required: true, description: '主机别名（见 ssh_otp_hosts）' },
      command: { type: 'string', required: true, description: '远程 shell 命令；多条命令用 ; 或 && 连接，减少连接轮次' },
      timeoutMs: { type: 'integer', description: '执行超时（毫秒），默认取插件配置，通常 120000' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          exitCode: { type: 'integer', required: true },
          stdout: { type: 'string', required: true },
          stderr: { type: 'string', required: true },
          truncated: { type: 'boolean', required: true },
          spillPath: { type: 'string' },
          bytesTotal: { type: 'integer' },
          timedOut: { type: 'boolean' },
        },
      },
      render: (_args, value) => {
        const parts = [`[exit code: ${value.exitCode}${value.timedOut ? ', timed out' : ''}]`]
        if (value.stdout) parts.push(value.stdout.trimEnd())
        if (value.stderr) parts.push(`--- stderr ---\n${value.stderr.trimEnd()}`)
        if (value.truncated && value.spillPath) parts.push(`(full output: ${value.spillPath})`)
        return [{ type: 'text', text: parts.join('\n') }]
      },
    },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const { conn, timeoutMs } = await resolveConn(args.host)
      const payload = await bridge.request({
        op: 'exec',
        alias: args.host,
        conn,
        command: args.command,
        timeoutMs: args.timeoutMs ?? timeoutMs,
      })
      if (exec?.signal?.aborted) throw new Error('调用已取消；远程命令可能仍在运行')
      const out = shapeStream(payload.stdout, `${args.host}-stdout`)
      const err = shapeStream(payload.stderr, `${args.host}-stderr`)
      const result = {
        exitCode: typeof payload.exitCode === 'number' ? payload.exitCode : -1,
        stdout: out.text,
        stderr: err.text,
        truncated: out.truncated || err.truncated,
        timedOut: payload.timedOut === true,
      }
      const spill = out.spillPath ?? err.spillPath
      if (spill) result.spillPath = spill
      if (typeof payload.stdoutBytes === 'number') result.bytesTotal = payload.stdoutBytes
      return result
    },
  }))

  tools.push(defineTool({
    name: 'ssh_otp_upload',
    description: '把本机文件上传到远程主机（SFTP）。默认不覆盖远程同名文件，需要覆盖时显式传 overwrite=true。',
    parameters: {
      host: { type: 'string', required: true, description: '主机别名' },
      localPath: { type: 'string', required: true, description: '本机文件路径（建议用绝对路径）' },
      remotePath: { type: 'string', required: true, description: '远程目标路径（文件，不是目录）' },
      overwrite: { type: 'boolean', description: '远程已存在时是否覆盖，默认 false' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          bytes: { type: 'integer', required: true },
          remotePath: { type: 'string', required: true },
        },
      },
      render: (_args, value) => [{ type: 'text', text: `已上传 ${value.bytes} 字节 → ${value.remotePath}` }],
    },
    isConcurrencySafe: () => false,
    async execute(args) {
      const local = resolveLocalPath(args.localPath)
      if (!existsSync(local)) throw new Error(`本机文件不存在：${local}`)
      const { conn } = await resolveConn(args.host)
      const payload = await bridge.request({
        op: 'upload',
        alias: args.host,
        conn,
        localPath: local,
        remotePath: args.remotePath,
        overwrite: args.overwrite === true,
      })
      return { bytes: payload.bytes, remotePath: payload.remotePath }
    },
  }))

  tools.push(defineTool({
    name: 'ssh_otp_download',
    description: '把远程主机的文件下载到本机（SFTP）。默认不覆盖本机同名文件，需要覆盖时显式传 overwrite=true。',
    parameters: {
      host: { type: 'string', required: true, description: '主机别名' },
      remotePath: { type: 'string', required: true, description: '远程文件路径' },
      localPath: { type: 'string', required: true, description: '本机目标路径（建议用绝对路径；父目录会自动创建）' },
      overwrite: { type: 'boolean', description: '本机已存在时是否覆盖，默认 false' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          bytes: { type: 'integer', required: true },
          localPath: { type: 'string', required: true },
        },
      },
      render: (_args, value) => [{ type: 'text', text: `已下载 ${value.bytes} 字节 → ${value.localPath}` }],
    },
    isConcurrencySafe: () => false,
    async execute(args) {
      const local = resolveLocalPath(args.localPath)
      const { conn } = await resolveConn(args.host)
      const payload = await bridge.request({
        op: 'download',
        alias: args.host,
        conn,
        remotePath: args.remotePath,
        localPath: local,
        overwrite: args.overwrite === true,
      })
      return { bytes: payload.bytes, localPath: payload.localPath }
    },
  }))

  ctx.effect(() => {
    const disposers = tools.map((tool) => ctx.tools.register(tool))
    return () => {
      for (const dispose of disposers) dispose()
    }
  }, 'ssh-otp: tools')

  logger?.info?.(`ssh-otp: 已注册 ${tools.length} 个工具（配置文件 ${CONFIG_FILE}）`)
}
