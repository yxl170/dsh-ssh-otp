/**
 * dsh-ssh-otp 浏览器半边（手写，零构建）。
 *
 * 注意：这不是标准 ESM。DSH 的客户端加载器要求产物套在
 * window.__ModuleLoader__.load({ id, factory }) 里，id 必须等于包名。
 * 参考 dsh-mediacrawler/client.js 的零构建写法。
 *
 * 隔离原则：这里任何异常都不应该让 Web 界面起不来，
 * 所以注册与请求全部包在 try/catch 里；工具本身不依赖本文件。
 */
window.__ModuleLoader__.load({
  id: 'dsh-ssh-otp',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    const PKG = 'dsh-ssh-otp'
    const BASE = '/plugins/' + PKG

    async function api(path, init) {
      const response = await fetch(BASE + path, {
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        ...init,
      })
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(body.error || 'HTTP ' + response.status)
      return body
    }

    const css = {
      wrap: { display: 'grid', gap: '12px', padding: '12px 14px', border: '1px solid var(--dsw-alias-border-l1)', borderRadius: '10px', fontSize: '13px' },
      row: { display: 'grid', gridTemplateColumns: 'minmax(120px,1fr) auto', gap: '8px', alignItems: 'center' },
      input: { padding: '4px 6px', borderRadius: '6px', border: '1px solid var(--dsw-alias-border-l2)', background: 'var(--dsw-alias-bg-base)', color: 'inherit', width: '100%' },
      badgeOk: { color: 'var(--dsw-alias-text-success, #2e9e44)', fontWeight: 600 },
      badgeBad: { color: 'var(--dsw-alias-text-warning, #c47f17)', fontWeight: 600 },
      muted: { opacity: 0.7, fontSize: '12px' },
      sectionTitle: { margin: '4px 0 0', fontSize: '12px', textTransform: 'uppercase', letterSpacing: '0.04em', opacity: 0.6 },
    }

    function refRows(hosts) {
      const map = new Map()
      for (const host of hosts) {
        if (host.passwordRef) {
          const ready = host.passwordReady === true
          map.set(host.passwordRef, map.get(host.passwordRef) === false ? false : ready)
        }
        if (host.totpRef) {
          const ready = host.totpReady === true
          map.set(host.totpRef, map.get(host.totpRef) === false ? false : ready)
        }
      }
      return [...map.entries()].map(([ref, ready]) => ({ ref, ready }))
    }

    function SettingsPanel(props) {
      // 这个 slot 会出现在每个插件的详情页，必须自己过滤
      try {
        const subject = props && props.subject
        const pkg = subject && (subject.kind === 'bundle' || subject.kind === 'row')
          ? subject.pkg && subject.pkg.name
          : undefined
        if (pkg !== undefined && pkg !== PKG) return null
      } catch {
        /* 结构不认识时按本插件处理 */
      }

      const { useCallback, useEffect, useState } = React
      const [hosts, setHosts] = useState([])
      const [draft, setDraft] = useState({})
      const [status, setStatus] = useState('')
      const [busy, setBusy] = useState('')
      const [loaded, setLoaded] = useState(false)

      const refresh = useCallback(async () => {
        try {
          const body = await api('/state')
          setHosts(Array.isArray(body.hosts) ? body.hosts : [])
          setStatus('')
        } catch (error) {
          setStatus('读取状态失败：' + String(error.message || error))
        } finally {
          setLoaded(true)
        }
      }, [])

      useEffect(() => { refresh() }, [refresh])

      const save = useCallback(async (ref) => {
        const value = draft[ref] || ''
        if (value === '') return
        setBusy(ref)
        try {
          await api('/secret', { method: 'POST', body: JSON.stringify({ ref, value }) })
          setDraft((prev) => ({ ...prev, [ref]: '' }))
          setStatus(ref + ' 已保存到宿主凭据库')
          await refresh()
        } catch (error) {
          setStatus('保存失败：' + String(error.message || error))
        } finally {
          setBusy('')
        }
      }, [draft, refresh])

      const test = useCallback(async (alias) => {
        setBusy('test:' + alias)
        try {
          const body = await api('/test', { method: 'POST', body: JSON.stringify({ alias }) })
          setStatus(body.ok
            ? alias + ' 连接正常：' + String(body.output || '').replace(/\s+/g, ' ')
            : alias + ' 连接失败：' + String(body.error || ('exit ' + body.exitCode)))
        } catch (error) {
          setStatus(alias + ' 测试出错：' + String(error.message || error))
        } finally {
          setBusy('')
        }
      }, [])

      const refs = refRows(hosts)

      const children = [
        h('h4', { key: 'title', style: { margin: 0, fontSize: '13px' } }, 'SSH 连接（含二次验证）'),
      ]

      if (!loaded) {
        children.push(h('div', { key: 'loading', style: css.muted }, '读取中…'))
      }

      if (refs.length > 0) {
        children.push(h('div', { key: 'refs-title', style: css.sectionTitle }, '凭据'))
        for (const item of refs) {
          children.push(h('div', { key: 'ref-' + item.ref, style: css.row },
            h('div', null,
              h('div', { style: { fontFamily: 'ui-monospace, monospace' } }, item.ref),
              h('div', { style: item.ready ? css.badgeOk : css.badgeBad }, item.ready ? '已配置' : '未配置')),
            h('div', { style: { display: 'flex', gap: '6px', minWidth: '260px' } },
              h('input', {
                type: 'password',
                placeholder: item.ready ? '输入新值以覆盖' : '粘贴密钥后保存',
                value: draft[item.ref] || '',
                disabled: busy !== '',
                onChange: (event) => setDraft((prev) => ({ ...prev, [item.ref]: event.target.value })),
                style: css.input,
              }),
              h('button', {
                type: 'button',
                disabled: busy !== '' || !(draft[item.ref] || ''),
                onClick: () => save(item.ref),
              }, busy === item.ref ? '保存中…' : '保存'))))
        }
      }

      if (hosts.length > 0) {
        children.push(h('div', { key: 'hosts-title', style: css.sectionTitle }, '主机'))
        for (const host of hosts) {
          const marks = [host.passwordReady ? '密码✓' : '密码✗']
          if (host.authKind === 'password_totp') marks.push(host.totpReady ? 'TOTP✓' : 'TOTP✗')
          children.push(h('div', { key: 'host-' + host.alias, style: css.row },
            h('div', null,
              h('div', { style: { fontWeight: 600 } }, host.alias),
              h('div', { style: css.muted }, host.target + '  ·  ' + host.authKind + '  ·  ' + marks.join(' '))),
            h('button', {
              type: 'button',
              disabled: busy !== '',
              onClick: () => test(host.alias),
            }, busy === 'test:' + host.alias ? '测试中…' : '测试连接')))
        }
        children.push(h('div', { key: 'hint', style: css.muted },
          '测试连接会真实登录一次；带二次验证的主机会消耗一个验证码，两次测试之间建议隔 30 秒。'))
      } else if (loaded) {
        children.push(h('div', { key: 'empty', style: css.muted },
          '还没有配置主机。编辑 ~/.dsh/dsh-ssh-otp.json 的 hosts 后刷新本页。'))
      }

      if (status) {
        children.push(h('p', { key: 'status', role: 'status', style: { margin: 0, fontSize: '12px' } }, status))
      }

      children.push(h('div', { key: 'actions' },
        h('button', { type: 'button', disabled: busy !== '', onClick: refresh }, '刷新')))

      return h('section', { style: css.wrap }, children)
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        try {
          ctx.slots.inject('plugins.detail.section', () => ctx.slots.register(
            { name: 'plugins.detail.section', id: PKG },
            SettingsPanel,
          ))
        } catch (error) {
          // 注册失败只影响这一块面板，绝不让 Web 界面起不来
          console.warn('[dsh-ssh-otp] 设置面板注册失败：', error)
        }
      },
    }
  },
})
