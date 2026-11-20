/**
 * dsh-ssh-otp 浏览器半边（手写，零构建）。
 *
 * 注意：这不是标准 ESM。DSH 的客户端加载器要求产物套在
 * window.__ModuleLoader__.load({ id, factory }) 里，id 必须等于包名。
 *
 * 设置页做三件事：
 *   1. 展示主机与凭据就绪状态；
 *   2. 录入/覆盖两个密钥（走后端 /secret，只写宿主凭据库）；
 *   3. 增删改主机与全局配置（走后端 /config，原子写 dsh-ssh-otp.json）。
 * 任何异常都只影响这一块面板，不让 Web 界面起不来。
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
      input: { padding: '4px 6px', borderRadius: '6px', border: '1px solid var(--dsw-alias-border-l2)', background: 'var(--dsw-alias-bg-base)', color: 'inherit', width: '100%', boxSizing: 'border-box' },
      form: { display: 'grid', gridTemplateColumns: 'auto minmax(180px, 1fr)', gap: '8px 10px', alignItems: 'center', padding: '10px 12px', border: '1px dashed var(--dsw-alias-border-l2)', borderRadius: '8px' },
      actions: { display: 'flex', gap: '6px', flexWrap: 'wrap' },
      badgeOk: { color: 'var(--dsw-alias-text-success, #2e9e44)', fontWeight: 600 },
      badgeBad: { color: 'var(--dsw-alias-text-warning, #c47f17)', fontWeight: 600 },
      muted: { opacity: 0.7, fontSize: '12px' },
      sectionTitle: { margin: '4px 0 0', fontSize: '12px', textTransform: 'uppercase', letterSpacing: '0.04em', opacity: 0.6 },
      btn: { padding: '4px 10px', borderRadius: '6px', border: '1px solid var(--dsw-alias-border-l2)', background: 'transparent', color: 'inherit', cursor: 'pointer' },
      primary: { padding: '4px 12px', borderRadius: '6px', border: '1px solid var(--dsw-alias-border-l2)', background: 'var(--dsw-alias-bg-base)', color: 'inherit', cursor: 'pointer', fontWeight: 600 },
      danger: { padding: '4px 10px', borderRadius: '6px', border: '1px solid var(--dsw-alias-border-l2)', background: 'transparent', color: 'var(--dsw-alias-text-danger, #c0392b)', cursor: 'pointer' },
      label: { opacity: 0.8 },
    }

    function sanitizeRef(alias, suffix) {
      const base = String(alias || '').replace(/[^A-Za-z0-9_]/g, '_')
      const safe = /^[A-Za-z_]/.test(base) ? base.toUpperCase() : 'HOST_' + base.toUpperCase()
      return safe + '_' + suffix
    }

    function refRows(hosts, refStates) {
      const fromState = new Map((refStates || []).map((item) => [item.ref, item.configured === true]))
      const map = new Map()
      for (const host of hosts || []) {
        if (host.passwordRef) map.set(host.passwordRef, fromState.get(host.passwordRef) ?? host.passwordReady === true)
        if (host.totpRef) map.set(host.totpRef, fromState.get(host.totpRef) ?? host.totpReady === true)
      }
      for (const [ref, configured] of fromState) if (!map.has(ref)) map.set(ref, configured)
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
      const [refs, setRefs] = useState([])
      const [config, setConfig] = useState(null)
      const [configPath, setConfigPath] = useState('')
      const [draft, setDraft] = useState({})
      const [status, setStatus] = useState('')
      const [busy, setBusy] = useState('')
      const [loaded, setLoaded] = useState(false)
      const [editor, setEditor] = useState(null)
      const [advanced, setAdvanced] = useState(null)

      const refresh = useCallback(async () => {
        try {
          const [stateBody, configBody] = await Promise.all([api('/state'), api('/config')])
          setHosts(Array.isArray(stateBody.hosts) ? stateBody.hosts : [])
          setRefs(Array.isArray(stateBody.refs) ? stateBody.refs : [])
          setConfig(configBody.config || null)
          setConfigPath(configBody.configPath || '')
          setStatus('')
        } catch (error) {
          setStatus('读取状态失败：' + String(error.message || error))
        } finally {
          setLoaded(true)
        }
      }, [])

      useEffect(() => { refresh() }, [refresh])

      const saveSecret = useCallback(async (ref) => {
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

      const saveHosts = useCallback(async (nextHosts, message) => {
        setBusy('config')
        try {
          const body = await api('/config', { method: 'POST', body: JSON.stringify({ hosts: nextHosts }) })
          setConfig(body.config || null)
          setEditor(null)
          setStatus(message || '主机配置已保存')
          await refresh()
          setStatus(message || '主机配置已保存')
        } catch (error) {
          setStatus('保存配置失败：' + String(error.message || error))
        } finally {
          setBusy('')
        }
      }, [refresh])

      const openNew = useCallback(() => {
        setEditor({ mode: 'new', alias: '', host: '', port: '22', user: '', authKind: 'password_totp', passwordRef: '', totpRef: '' })
        setStatus('')
      }, [])

      const openEdit = useCallback((item) => {
        setEditor({
          mode: 'edit',
          alias: item.alias,
          host: item.host,
          port: String(item.port ?? 22),
          user: item.user,
          authKind: item.authKind,
          passwordRef: item.passwordRef,
          totpRef: item.totpRef || '',
        })
        setStatus('')
      }, [])

      const editField = useCallback((key, value) => {
        setEditor((prev) => (prev === null ? prev : { ...prev, [key]: value }))
      }, [])

      const commitEditor = useCallback(async () => {
        if (editor === null || config === null) return
        const trimmedAlias = String(editor.alias).trim()
        if (trimmedAlias === '') { setStatus('请填写别名（例如 cluster-a）'); return }
        if (editor.mode === 'new' && (config.hosts || []).some((host) => host.alias === trimmedAlias)) {
          setStatus(`别名 ${trimmedAlias} 已存在；请换一个，或直接编辑那台主机。`)
          return
        }
        const item = {
          alias: trimmedAlias,
          host: String(editor.host).trim(),
          port: editor.port === '' ? 22 : Number(editor.port),
          user: String(editor.user).trim(),
          authKind: editor.authKind,
          passwordRef: String(editor.passwordRef).trim() || sanitizeRef(trimmedAlias, 'SSH_PASSWORD'),
          totpRef: String(editor.totpRef).trim() || (editor.authKind === 'password_totp' ? sanitizeRef(trimmedAlias, 'TOTP_SEED') : ''),
        }
        const next = (config.hosts || []).filter((host) => host.alias !== (editor.mode === 'edit' ? editor.alias : trimmedAlias))
        next.push(item)
        await saveHosts(next, editor.mode === 'edit' ? `已更新主机 ${trimmedAlias}` : `已添加主机 ${trimmedAlias}，请在上面录入对应凭据`)
      }, [editor, config, saveHosts])

      const removeHost = useCallback(async (alias) => {
        if (config === null) return
        if (typeof window !== 'undefined' && typeof window.confirm === 'function' && !window.confirm(`删除主机 ${alias}？配置文件会立即更新。`)) return
        const next = (config.hosts || []).filter((host) => host.alias !== alias)
        await saveHosts(next, `已删除主机 ${alias}`)
      }, [config, saveHosts])

      const openAdvanced = useCallback(() => {
        if (advanced !== null || config === null) return
        setAdvanced({
          pythonPath: config.pythonPath || '',
          pythonResolved: config.pythonResolved || '',
          pythonIssue: config.pythonIssue || '',
          servicePath: config.servicePath || '',
          idleMinutes: String(config.idleMinutes ?? 10),
          timeoutMs: String(config.timeoutMs ?? 120000),
        })
      }, [advanced, config])

      const editAdvanced = useCallback((key, value) => {
        setAdvanced((prev) => (prev === null ? prev : { ...prev, [key]: value }))
      }, [])

      const saveAdvanced = useCallback(async () => {
        if (advanced === null) return
        setBusy('advanced')
        try {
          const body = await api('/config', {
            method: 'POST',
            body: JSON.stringify({
              timeoutMs: Number(advanced.timeoutMs),
            }),
          })
          setConfig(body.config || null)
          setAdvanced(null)
          setStatus('运行配置已保存')
          await refresh()
        } catch (error) {
          setStatus('保存配置失败：' + String(error.message || error))
        } finally {
          setBusy('')
        }
      }, [advanced, refresh])

      const rows = refRows(hosts, refs)
      const busyNow = busy !== ''

      const children = [
        h('h4', { key: 'title', style: { margin: 0, fontSize: '13px' } }, 'SSH 连接（含二次验证）'),
      ]

      if (!loaded) children.push(h('div', { key: 'loading', style: css.muted }, '读取中…'))

      if (rows.length > 0) {
        children.push(h('div', { key: 'refs-title', style: css.sectionTitle }, '凭据'))
        for (const item of rows) {
          children.push(h('div', { key: 'ref-' + item.ref, style: css.row },
            h('div', null,
              h('div', { style: { fontFamily: 'ui-monospace, monospace' } }, item.ref),
              h('div', { style: item.ready ? css.badgeOk : css.badgeBad }, item.ready ? '已配置' : '未配置')),
            h('div', { style: { display: 'flex', gap: '6px', minWidth: '260px' } },
              h('input', {
                type: 'password',
                placeholder: item.ready ? '输入新值以覆盖' : '粘贴密钥后保存',
                value: draft[item.ref] || '',
                disabled: busyNow,
                onChange: (event) => setDraft((prev) => ({ ...prev, [item.ref]: event.target.value })),
                style: css.input,
              }),
              h('button', {
                type: 'button',
                style: css.btn,
                disabled: busyNow || !(draft[item.ref] || ''),
                onClick: () => saveSecret(item.ref),
              }, busy === item.ref ? '保存中…' : '保存'))))
        }
      }

      children.push(h('div', { key: 'hosts-title', style: css.sectionTitle }, '主机连接'))
      if (config !== null && (config.hosts || []).length > 0) {
        for (const item of config.hosts) {
          const state = hosts.find((host) => host.alias === item.alias)
          const marks = state
            ? [(state.passwordReady ? '密码✓' : '密码✗')].concat(state.authKind === 'password_totp' ? [state.totpReady ? 'TOTP✓' : 'TOTP✗'] : [])
            : []
          children.push(h('div', { key: 'host-' + item.alias, style: css.row },
            h('div', null,
              h('div', { style: { fontWeight: 600 } }, item.alias),
              h('div', { style: css.muted },
                `${item.user}@${item.host}:${item.port}  ·  ${item.authKind}` + (marks.length ? '  ·  ' + marks.join(' ') : ''))),
            h('div', { style: css.actions },
              h('button', { type: 'button', style: css.btn, disabled: busyNow, onClick: () => test(item.alias) },
                busy === 'test:' + item.alias ? '测试中…' : '测试连接'),
              h('button', { type: 'button', style: css.btn, disabled: busyNow, onClick: () => openEdit(item) }, '编辑'),
              h('button', { type: 'button', style: css.danger, disabled: busyNow, onClick: () => removeHost(item.alias) }, '删除'))))
        }
      } else if (loaded) {
        children.push(h('div', { key: 'empty', style: css.muted }, '还没有主机。点「新增主机」填地址和账号，保存后录入凭据即可。'))
      }

      if (editor !== null) {
        const isTotp = editor.authKind === 'password_totp'
        const aliasForRefs = String(editor.alias || '').trim() || 'host'
        children.push(h('div', { key: 'editor', style: css.form },
          h('label', { style: css.label }, '别名'),
          h('input', { value: editor.alias, disabled: editor.mode === 'edit' || busyNow, placeholder: '例如 cluster-a', onChange: (e) => editField('alias', e.target.value), style: css.input }),
          h('label', { style: css.label }, '主机'),
          h('input', { value: editor.host, disabled: busyNow, placeholder: '例如 10.0.0.10 或 login.example.org', onChange: (e) => editField('host', e.target.value), style: css.input }),
          h('label', { style: css.label }, '端口'),
          h('input', { value: editor.port, disabled: busyNow, placeholder: '22', onChange: (e) => editField('port', e.target.value), style: css.input }),
          h('label', { style: css.label }, '用户名'),
          h('input', { value: editor.user, disabled: busyNow, placeholder: 'ssh 登录名', onChange: (e) => editField('user', e.target.value), style: css.input }),
          h('label', { style: css.label }, '认证方式'),
          h('select', {
            value: editor.authKind,
            disabled: busyNow,
            onChange: (e) => editField('authKind', e.target.value),
            style: css.input,
          },
            h('option', { value: 'password_totp' }, '密码 + 动态验证码'),
            h('option', { value: 'password' }, '仅密码')),
          h('label', { style: css.label }, '密码凭据名'),
          h('input', {
            value: editor.passwordRef,
            disabled: busyNow,
            placeholder: sanitizeRef(aliasForRefs, 'SSH_PASSWORD'),
            onChange: (e) => editField('passwordRef', e.target.value),
            style: css.input,
          }),
          h('label', { style: css.label }, 'TOTP 凭据名'),
          h('input', {
            value: editor.totpRef,
            disabled: busyNow || !isTotp,
            placeholder: isTotp ? sanitizeRef(aliasForRefs, 'TOTP_SEED') : '仅二次验证时需要',
            onChange: (e) => editField('totpRef', e.target.value),
            style: css.input,
          }),
          h('div', { style: { gridColumn: '1 / -1' } },
            h('div', { style: css.muted }, '凭据名留空会按别名自动生成；凭据值在上方「凭据」区录入，保存配置不会写任何密钥。'),
            h('div', { style: { ...css.actions, marginTop: '8px' } },
              h('button', { type: 'button', style: css.primary, disabled: busyNow, onClick: commitEditor }, busy === 'config' ? '保存中…' : '保存主机'),
              h('button', { type: 'button', style: css.btn, disabled: busyNow, onClick: () => setEditor(null) }, '取消')))))
      } else {
        children.push(h('div', { key: 'add-host' },
          h('button', { type: 'button', style: css.primary, disabled: busyNow, onClick: openNew }, '新增主机')))
      }

      children.push(h('details', { key: 'advanced', style: css.muted, onToggle: openAdvanced },
        h('summary', { style: { cursor: 'pointer' } }, '运行配置（路径只读，可改默认超时）'),
        advanced === null
          ? h('div', { style: css.muted }, '展开查看')
          : h('div', { style: { ...css.form, marginTop: '8px' } },
            h('span', null, 'Python 解释器'), h('code', null, advanced.pythonResolved || advanced.pythonPath || '（自动探测，尚未找到）'),
            h('span', null, '服务脚本（只读）'), h('code', null, advanced.servicePath),
            advanced.pythonIssue
              ? h('div', { style: { gridColumn: '1 / -1', color: 'var(--dsw-alias-text-warning, #c47f17)', whiteSpace: 'pre-wrap' } }, advanced.pythonIssue)
              : null,
            h('label', { style: css.label }, '默认超时（毫秒）'),
            h('input', { value: advanced.timeoutMs, disabled: busyNow, onChange: (e) => editAdvanced('timeoutMs', e.target.value), style: css.input }),
            h('div', { style: { gridColumn: '1 / -1', ...css.actions } },
              h('button', { type: 'button', style: css.primary, disabled: busyNow, onClick: saveAdvanced }, busy === 'advanced' ? '保存中…' : '保存运行配置'),
              h('button', { type: 'button', style: css.btn, disabled: busyNow, onClick: () => setAdvanced(null) }, '取消')))))

      if (configPath) {
        children.push(h('div', { key: 'config-path', style: css.muted }, '配置文件：' + configPath))
      }

      children.push(h('div', { key: 'hint', style: css.muted },
        '测试连接会真实登录一次；带二次验证的主机会消耗一个验证码，两次测试之间建议隔 30 秒。'))

      if (status) {
        children.push(h('p', { key: 'status', role: 'status', style: { margin: 0, fontSize: '12px' } }, status))
      }

      children.push(h('div', { key: 'actions', style: css.actions },
        h('button', { type: 'button', style: css.btn, disabled: busyNow, onClick: refresh }, '刷新')))

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
