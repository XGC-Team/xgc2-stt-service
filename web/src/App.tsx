import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent } from 'react'
import {
  AppShell,
  AudioCaptureControl,
  Button,
  FormField,
  FormGroup,
  Input,
  Modal,
  Panel,
  ProductBrand,
  SegmentedControl,
  StatusBadge,
  Topbar,
  type AudioCaptureState,
  type StatusTone,
} from '@xgc2/ui-react'
import {
  createApiKey,
  getApiKeys,
  getRuntimeSettings,
  getStatus,
  normalizeEndpoint,
  revokeApiKey,
  restartEngine,
  rotateApiKey,
  type ApiKeyRecord,
  type ConnectionSettings,
  type GpuSample,
  type RuntimeSettingsResponse,
  type RuntimeTuning,
  type ServiceStatus,
  updateRuntimeSettings,
} from './api'
import { MicrophoneStream, type StreamEvent } from './stream'

type Skin = 'light' | 'dark'

function initialConnection(): ConnectionSettings {
  const storedEndpoint = localStorage.getItem('xgc2-stt.endpoint')
  return {
    endpoint: storedEndpoint && storedEndpoint !== window.location.origin
      ? storedEndpoint
      : 'http://127.0.0.1:34897',
    apiKey: localStorage.getItem('xgc2-stt.apiKey') || '',
    outputScript: localStorage.getItem('xgc2-stt.outputScript') === 'original' ? 'original' : 'simplified',
    trimLeadingSilence: localStorage.getItem('xgc2-stt.trimLeadingSilence') !== 'false',
  }
}

function sparkline(samples: GpuSample[], key: keyof GpuSample, ceiling: number): string {
  const values = samples.map((sample) => Number(sample[key] ?? 0)).slice(-48)
  if (!values.length) return ''
  return values.map((value, index) => {
    const x = values.length === 1 ? 100 : index / (values.length - 1) * 100
    const y = 30 - Math.min(30, Math.max(0, value / ceiling * 30))
    return `${x.toFixed(2)},${y.toFixed(2)}`
  }).join(' ')
}

function formatMemory(bytes?: number): string {
  return bytes === undefined ? '—' : `${(bytes / (1024 ** 3)).toFixed(1)} GB`
}

export default function App() {
  const [connection, setConnection] = useState<ConnectionSettings>(initialConnection)
  const [draftConnection, setDraftConnection] = useState<ConnectionSettings>(initialConnection)
  const [status, setStatus] = useState<ServiceStatus | null>(null)
  const [statusError, setStatusError] = useState('')
  const [apiKeys, setApiKeys] = useState<ApiKeyRecord[]>([])
  const [apiKeyError, setApiKeyError] = useState('')
  const [runtimeSettings, setRuntimeSettings] = useState<RuntimeSettingsResponse | null>(null)
  const [draftRuntime, setDraftRuntime] = useState<RuntimeTuning | null>(null)
  const [settingsError, setSettingsError] = useState('')
  const [captureState, setCaptureState] = useState<AudioCaptureState>('idle')
  const captureStateRef = useRef<AudioCaptureState>('idle')
  const [streamState, setStreamState] = useState('')
  const [partialText, setPartialText] = useState('')
  const [partialStableText, setPartialStableText] = useState('')
  const [transcript, setTranscript] = useState('')
  const [captureError, setCaptureError] = useState('')
  const [waveformLevels, setWaveformLevels] = useState<readonly number[]>([])
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [keyDialogOpen, setKeyDialogOpen] = useState(false)
  const [keyName, setKeyName] = useState('')
  const [issuedSecret, setIssuedSecret] = useState('')
  const [skin, setSkinState] = useState<Skin>(() => localStorage.getItem('xgc2-stt.skin') === 'dark' ? 'dark' : 'light')
  const streamRef = useRef<MicrophoneStream | null>(null)
  const acceptStreamEventsRef = useRef(false)
  const browserKeyProvisioningRef = useRef(false)
  if (!streamRef.current) streamRef.current = new MicrophoneStream()

  const transitionCapture = useCallback((next: AudioCaptureState) => {
    captureStateRef.current = next
    setCaptureState(next)
  }, [])

  const applySkin = useCallback((next: Skin) => {
    setSkinState(next)
    document.documentElement.dataset.skin = next
    localStorage.setItem('xgc2-stt.skin', next)
  }, [])

  const refreshStatus = useCallback(async () => {
    const [statusResult, keysResult, runtimeResult] = await Promise.allSettled([
      getStatus(),
      getApiKeys(),
      getRuntimeSettings(),
    ])
    if (statusResult.status === 'fulfilled') {
      setStatus(statusResult.value)
      setStatusError('')
    } else {
      const error = statusResult.reason
      setStatusError(error instanceof Error ? error.message : '状态请求失败')
    }
    if (keysResult.status === 'fulfilled') {
      setApiKeys(keysResult.value)
      setApiKeyError('')
    } else {
      const error = keysResult.reason
      setApiKeyError(error instanceof Error ? error.message : 'API Key 请求失败')
    }
    if (runtimeResult.status === 'fulfilled') {
      setRuntimeSettings(runtimeResult.value)
      setDraftRuntime((current) => current || runtimeResult.value.values)
    }
  }, [])

  useEffect(() => {
    document.documentElement.dataset.skin = skin
    void refreshStatus()
    const pollTimer = window.setInterval(() => void refreshStatus(), 5000)
    return () => window.clearInterval(pollTimer)
  }, [refreshStatus, skin])

  useEffect(() => {
    if (status?.authentication !== 'api-key' || connection.apiKey || browserKeyProvisioningRef.current) return
    browserKeyProvisioningRef.current = true
    void createApiKey('webui-local').then((created) => {
      const next = { ...connection, apiKey: created.secret }
      setConnection(next)
      setDraftConnection(next)
      localStorage.setItem('xgc2-stt.apiKey', created.secret)
      setApiKeyError('')
      void refreshStatus()
    }).catch((error) => {
      browserKeyProvisioningRef.current = false
      setApiKeyError(error instanceof Error ? error.message : 'WebUI Key 创建失败')
    })
  }, [connection, refreshStatus, status?.authentication])

  useEffect(() => () => {
    void streamRef.current?.cancel()
  }, [])

  const engineLabel = useMemo(() => {
    const state = status?.engine.state
    if (state === 'ready') return '就绪'
    if (state === 'loading') return '加载模型'
    if (state === 'error') return '错误'
    if (state === 'idle') return '待加载'
    return statusError ? '离线' : '连接中'
  }, [status, statusError])

  const engineTone: StatusTone = status?.engine.state === 'ready'
    ? 'success'
    : status?.engine.state === 'error' || statusError
      ? 'danger'
      : 'warning'

  const recordLabel = captureState === 'connecting'
    ? '连接中'
    : captureState === 'recording'
      ? '停止并转写'
      : captureState === 'finalizing'
        ? '生成结果'
        : '开始录音'

  const waveformStyle = useMemo(() => Object.fromEntries(
    waveformLevels.slice(0, 24).map((level, index) => [
      `--stt-wave-${index + 1}`,
      String(Math.max(0.12, Math.min(1, level))),
    ]),
  ) as CSSProperties, [waveformLevels])

  const handleStreamEvent = useCallback((event: StreamEvent) => {
    if (!acceptStreamEventsRef.current) return
    if (event.type === 'session.started') {
      setStreamState('已连接')
      return
    }
    if (event.type === 'model.loading') {
      setStreamState('加载模型')
      return
    }
    if (event.type === 'transcript.partial') {
      setPartialStableText(event.stable_text || '')
      setPartialText(event.unstable_text ?? event.text ?? '')
      setStreamState('转写中')
      return
    }
    if (event.type === 'transcript.final') {
      const normalized = (event.text || '').trim()
      if (normalized) setTranscript((current) => current ? `${current}\n${normalized}` : normalized)
      setPartialStableText('')
      setPartialText('')
      setStreamState(captureStateRef.current === 'recording' ? '录音中' : '')
      if (event.session_complete !== false && captureStateRef.current === 'finalizing') {
        streamRef.current?.close()
        transitionCapture('idle')
      }
      return
    }
    if (event.type === 'error') setCaptureError(event.message || '流式转写失败')
  }, [transitionCapture])

  const toggleCapture = useCallback(async () => {
    setCaptureError('')
    if (captureStateRef.current === 'recording') {
      transitionCapture('finalizing')
      setStreamState('生成结果')
      await streamRef.current?.stop()
      return
    }
    if (captureStateRef.current !== 'idle') return
    if (status?.engine.state !== 'ready') {
      setCaptureError('模型尚未就绪')
      return
    }
    transitionCapture('connecting')
    acceptStreamEventsRef.current = true
    try {
      await streamRef.current?.start(
        connection,
        handleStreamEvent,
        (reason) => {
          if (captureStateRef.current !== 'idle' && captureStateRef.current !== 'finalizing') setCaptureError(reason)
          transitionCapture('idle')
          setStreamState('')
        },
        setWaveformLevels,
      )
      transitionCapture('recording')
      setStreamState('录音中')
    } catch (error) {
      acceptStreamEventsRef.current = false
      transitionCapture('idle')
      setStreamState('')
      setCaptureError(error instanceof Error ? error.message : '无法启动录音')
    }
  }, [connection, handleStreamEvent, status?.engine.state, transitionCapture])

  const cancelCapture = useCallback(async () => {
    acceptStreamEventsRef.current = false
    await streamRef.current?.cancel()
    transitionCapture('idle')
    setPartialText('')
    setPartialStableText('')
    setStreamState('')
    setWaveformLevels([])
  }, [transitionCapture])

  const clearTranscript = useCallback(async () => {
    acceptStreamEventsRef.current = false
    setTranscript('')
    setPartialText('')
    setPartialStableText('')
    setCaptureError('')
    if (captureStateRef.current === 'recording') {
      try {
        await streamRef.current?.clearSession()
        acceptStreamEventsRef.current = true
        setStreamState('录音中')
      } catch (error) {
        await streamRef.current?.cancel()
        transitionCapture('idle')
        setStreamState('')
        setCaptureError(error instanceof Error ? error.message : '无法重置识别会话')
      }
      return
    }
    setStreamState('')
  }, [transitionCapture])

  const copyTranscript = useCallback(async () => {
    const preview = `${partialStableText}${partialText}`
    const value = [transcript, preview].filter(Boolean).join('\n')
    if (value) await navigator.clipboard.writeText(value)
  }, [partialStableText, partialText, transcript])

  const openSettings = () => {
    setDraftConnection(connection)
    setDraftRuntime(runtimeSettings?.values || null)
    setSettingsError('')
    setSettingsOpen(true)
  }

  const saveSettings = async (event: FormEvent) => {
    event.preventDefault()
    const next: ConnectionSettings = {
      endpoint: normalizeEndpoint(draftConnection.endpoint),
      apiKey: draftConnection.apiKey.trim(),
      outputScript: draftConnection.outputScript,
      trimLeadingSilence: draftConnection.trimLeadingSilence,
    }
    setConnection(next)
    localStorage.setItem('xgc2-stt.endpoint', next.endpoint)
    if (next.apiKey) localStorage.setItem('xgc2-stt.apiKey', next.apiKey)
    else localStorage.removeItem('xgc2-stt.apiKey')
    localStorage.setItem('xgc2-stt.outputScript', next.outputScript)
    localStorage.setItem('xgc2-stt.trimLeadingSilence', String(next.trimLeadingSilence))
    try {
      const updatedRuntime = draftRuntime ? await updateRuntimeSettings(draftRuntime) : runtimeSettings
      if (updatedRuntime) {
        setRuntimeSettings(updatedRuntime)
        setDraftRuntime(updatedRuntime.values)
      }
      setStatus(await getStatus())
      setStatusError('')
      setSettingsOpen(Boolean(updatedRuntime?.restart_required))
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : '设置保存失败')
    }
  }

  const restartModel = async () => {
    try {
      const updated = await restartEngine()
      setRuntimeSettings(updated)
      setDraftRuntime(updated.values)
      setSettingsError('')
      setSettingsOpen(false)
      await refreshStatus()
    } catch (error) {
      setSettingsError(error instanceof Error ? error.message : '模型重启失败')
    }
  }

  const hasTranscript = Boolean(transcript || partialStableText || partialText)

  const openKeyDialog = () => {
    setKeyName('')
    setIssuedSecret('')
    setKeyDialogOpen(true)
  }

  const issueKey = async (event: FormEvent) => {
    event.preventDefault()
    try {
      const created = await createApiKey(keyName)
      setIssuedSecret(created.secret)
      await refreshStatus()
    } catch (error) {
      setApiKeyError(error instanceof Error ? error.message : 'API Key 创建失败')
    }
  }

  const rotateKey = async (record: ApiKeyRecord) => {
    try {
      const rotated = await rotateApiKey(record.id)
      setKeyName(record.name)
      setIssuedSecret(rotated.secret)
      setKeyDialogOpen(true)
      await refreshStatus()
    } catch (error) {
      setApiKeyError(error instanceof Error ? error.message : 'API Key 轮换失败')
    }
  }

  const revokeKey = async (record: ApiKeyRecord) => {
    if (!window.confirm(`停用 ${record.name}？`)) return
    try {
      await revokeApiKey(record.id)
      await refreshStatus()
    } catch (error) {
      setApiKeyError(error instanceof Error ? error.message : 'API Key 停用失败')
    }
  }

  const useIssuedKey = () => {
    if (!issuedSecret) return
    const next = { ...connection, apiKey: issuedSecret }
    setConnection(next)
    setDraftConnection(next)
    localStorage.setItem('xgc2-stt.apiKey', issuedSecret)
  }

  const gpuMetrics = status?.gpu.available ? [
    {
      label: 'GPU',
      value: `${status.gpu.utilization_percent ?? 0}%`,
      percent: status.gpu.utilization_percent ?? 0,
      points: sparkline(status.gpu.history, 'utilization_percent', 100),
    },
    {
      label: '显存',
      value: `${formatMemory(status.gpu.memory_used_bytes)} / ${formatMemory(status.gpu.memory_total_bytes)}`,
      percent: status.gpu.memory_percent ?? 0,
      points: sparkline(status.gpu.history, 'memory_percent', 100),
    },
    {
      label: '温度',
      value: `${status.gpu.temperature_c ?? 0} °C`,
      percent: status.gpu.temperature_c ?? 0,
      points: sparkline(status.gpu.history, 'temperature_c', 100),
    },
    {
      label: '功耗',
      value: `${status.gpu.power_watts ?? 0} W`,
      percent: status.gpu.power_limit_watts
        ? (status.gpu.power_watts ?? 0) / status.gpu.power_limit_watts * 100
        : 0,
      points: sparkline(status.gpu.history, 'power_watts', status.gpu.power_limit_watts || 450),
    },
  ] : []

  return (
    <AppShell
      contentPadding="none"
      contentClassName="stt-content"
      topbar={(
        <Topbar
          leading={<ProductBrand product="STT" />}
          actions={<Button appearance="ghost" uiSize="compact" onClick={openSettings}>设置</Button>}
        />
      )}
    >
      <div className="stt-workspace">
        <Panel
          className="capture-panel"
          padding="none"
          title="实时转写"
          actions={streamState
            ? <StatusBadge status={captureState}>{streamState}</StatusBadge>
            : status?.engine.state !== 'ready' || statusError
              ? <StatusBadge status={statusError ? 'offline' : status?.engine.state || 'connecting'} tone={engineTone}>{engineLabel}</StatusBadge>
              : null}
        >
          <AudioCaptureControl
            style={waveformStyle}
            state={captureState}
            actionLabel={recordLabel}
            cancelLabel="取消"
            error={captureError}
            waveformLabel="麦克风输入活动"
            onAction={() => void toggleCapture()}
            onCancel={() => void cancelCapture()}
          />
        </Panel>

        <Panel
          className="transcript-panel"
          padding="none"
          title="转写结果"
          actions={(
            <>
              <Button appearance="ghost" uiSize="compact" disabled={!hasTranscript} onClick={() => void copyTranscript()}>复制</Button>
              <Button appearance="ghost" uiSize="compact" disabled={!hasTranscript && captureState === 'idle'} onClick={() => void clearTranscript()}>清空</Button>
            </>
          )}
        >
          <div className="transcript" aria-live="polite">
            <span className="final-text">{transcript}</span>
            {partialStableText ? (
              <span className="stable-partial-text">{transcript ? '\n' : ''}{partialStableText}</span>
            ) : null}
            {partialText ? (
              <span className="partial-text">{transcript && !partialStableText ? '\n' : ''}{partialText}</span>
            ) : null}
          </div>
        </Panel>

        <Panel className="runtime-panel" padding="none" title="运行状态">
          {status ? (
            <dl className="runtime-grid">
              <div><dt>模型</dt><dd>{status.engine.model}</dd></div>
              <div><dt>引擎</dt><dd>{status.engine.variant} · {status.engine.backend}</dd></div>
              <div><dt>推理</dt><dd>{status.engine.device} · {status.engine.compute_type}</dd></div>
              <div><dt>并发</dt><dd>{status.stream.active} / {status.stream.capacity}</dd></div>
              <div><dt>延迟</dt><dd>{status.stream.transcription_delay_ms} ms</dd></div>
              <div><dt>GPU</dt><dd>{status.engine.cuda_devices ?? '未知'} 个设备</dd></div>
              <div><dt>认证</dt><dd>{status.authentication}</dd></div>
              <div><dt>服务地址</dt><dd>{connection.endpoint}</dd></div>
            </dl>
          ) : <p className="status-error">{statusError || '读取中'}</p>}
        </Panel>

        <Panel className="gpu-panel" padding="none" title="GPU">
          {gpuMetrics.length ? (
            <div className="gpu-grid">
              {gpuMetrics.map((metric) => (
                <div className="gpu-metric" key={metric.label}>
                  <div><span>{metric.label}</span><strong>{metric.value}</strong></div>
                  <div className="gpu-bar"><i style={{ width: `${Math.min(100, Math.max(0, metric.percent))}%` }} /></div>
                  <svg viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
                    <polyline points={metric.points} />
                  </svg>
                </div>
              ))}
            </div>
          ) : <p className="status-error">{status?.gpu.error || statusError}</p>}
        </Panel>

        <Panel
          className="api-panel"
          padding="none"
          title="API"
          actions={<Button appearance="ghost" uiSize="compact" onClick={openKeyDialog}>新建 Key</Button>}
        >
          <div className="api-key-table" role="table" aria-label="API Key 使用情况">
            <div className="api-key-row api-key-head" role="row">
              <span>名称</span><span>Key</span><span>请求</span><span>音频</span><span>活跃</span><span />
            </div>
            {apiKeys.map((record) => (
              <div className="api-key-row" role="row" key={record.id} data-disabled={!record.enabled}>
                <strong>{record.name}</strong>
                <code>{record.prefix}…</code>
                <span>{record.request_count}</span>
                <span>{(record.audio_seconds / 60).toFixed(1)} min</span>
                <span>{record.active_sessions}</span>
                <span className="api-key-actions">
                  <Button appearance="ghost" uiSize="compact" onClick={() => void rotateKey(record)}>轮换</Button>
                  <Button appearance="ghost" uiSize="compact" disabled={!record.enabled} onClick={() => void revokeKey(record)}>停用</Button>
                </span>
              </div>
            ))}
          </div>
          {apiKeyError ? <p className="status-error">{apiKeyError}</p> : null}
        </Panel>
      </div>

      <Modal
        open={settingsOpen}
        title="设置"
        closeLabel="关闭设置"
        onClose={() => setSettingsOpen(false)}
        actions={(
          <>
            <Button onClick={() => setSettingsOpen(false)}>取消</Button>
            {runtimeSettings?.restart_required ? <Button onClick={() => void restartModel()}>重启模型</Button> : null}
            <Button form="stt-settings" type="submit" tone="primary">保存</Button>
          </>
        )}
      >
        <form id="stt-settings" className="settings-form" onSubmit={(event) => void saveSettings(event)}>
          <section className="settings-section">
            <h3>客户端</h3>
          <FormField label="API 地址" required>
            <Input
              type="url"
              required
              value={draftConnection.endpoint}
              onValueChange={(endpoint) => setDraftConnection((current) => ({ ...current, endpoint }))}
            />
          </FormField>
          <FormField label="API Key">
            <Input
              type="password"
              autoComplete="off"
              value={draftConnection.apiKey}
              onValueChange={(apiKey) => setDraftConnection((current) => ({ ...current, apiKey }))}
            />
          </FormField>
          <FormGroup label="中文输出">
            <SegmentedControl
              ariaLabel="中文输出"
              value={draftConnection.outputScript}
              options={[{ label: '简体', value: 'simplified' }, { label: '原样', value: 'original' }]}
              onValueChange={(value) => setDraftConnection((current) => ({
                ...current,
                outputScript: value as ConnectionSettings['outputScript'],
              }))}
            />
          </FormGroup>
          <FormGroup label="开头静音">
            <SegmentedControl
              ariaLabel="开头静音"
              value={draftConnection.trimLeadingSilence ? 'trim' : 'keep'}
              options={[{ label: '裁剪', value: 'trim' }, { label: '保留', value: 'keep' }]}
              onValueChange={(value) => setDraftConnection((current) => ({
                ...current,
                trimLeadingSilence: value === 'trim',
              }))}
            />
          </FormGroup>
          </section>
          {draftRuntime ? (
            <section className="settings-section">
              <h3>服务</h3>
              <FormField label="静音定稿 · 新会话">
                <Input
                  type="number"
                  value={String(draftRuntime.silence_commit_ms)}
                  onValueChange={(value) => setDraftRuntime((current) => current
                    ? { ...current, silence_commit_ms: Number(value) }
                    : current)}
                />
              </FormField>
              <FormField label="活跃流上限 · 即时">
                <Input
                  type="number"
                  value={String(draftRuntime.max_active_streams)}
                  onValueChange={(value) => setDraftRuntime((current) => current
                    ? { ...current, max_active_streams: Number(value) }
                    : current)}
                />
              </FormField>
              <FormField label="GPU 显存比例 · 重启模型">
                <Input
                  type="number"
                  value={String(draftRuntime.gpu_memory_utilization)}
                  onValueChange={(value) => setDraftRuntime((current) => current
                    ? { ...current, gpu_memory_utilization: Number(value) }
                    : current)}
                />
              </FormField>
              {status?.engine.variant === 'qwen' ? (
                <>
                  <FormField label="Qwen 分块秒数 · 重启模型">
                    <Input
                      type="number"
                      value={String(draftRuntime.qwen_chunk_size_seconds)}
                      onValueChange={(value) => setDraftRuntime((current) => current
                        ? { ...current, qwen_chunk_size_seconds: Number(value) }
                        : current)}
                    />
                  </FormField>
                  <FormField label="Qwen 回改分块 · 重启模型">
                    <Input
                      type="number"
                      value={String(draftRuntime.qwen_unfixed_chunk_num)}
                      onValueChange={(value) => setDraftRuntime((current) => current
                        ? { ...current, qwen_unfixed_chunk_num: Number(value) }
                        : current)}
                    />
                  </FormField>
                  <FormField label="Qwen 回改 Token · 重启模型">
                    <Input
                      type="number"
                      value={String(draftRuntime.qwen_unfixed_token_num)}
                      onValueChange={(value) => setDraftRuntime((current) => current
                        ? { ...current, qwen_unfixed_token_num: Number(value) }
                        : current)}
                    />
                  </FormField>
                </>
              ) : (
                <>
                  <FormField label="最大模型长度 · 重启模型">
                    <Input
                      type="number"
                      value={String(draftRuntime.max_model_len)}
                      onValueChange={(value) => setDraftRuntime((current) => current
                        ? { ...current, max_model_len: Number(value) }
                        : current)}
                    />
                  </FormField>
                  <FormField label="最大序列数 · 重启模型">
                    <Input
                      type="number"
                      value={String(draftRuntime.max_num_seqs)}
                      onValueChange={(value) => setDraftRuntime((current) => current
                        ? { ...current, max_num_seqs: Number(value) }
                        : current)}
                    />
                  </FormField>
                  <FormGroup label="Eager 模式 · 重启模型">
                    <SegmentedControl
                      ariaLabel="Eager 模式"
                      value={draftRuntime.vllm_enforce_eager ? 'on' : 'off'}
                      options={[{ label: '关闭', value: 'off' }, { label: '开启', value: 'on' }]}
                      onValueChange={(value) => setDraftRuntime((current) => current
                        ? { ...current, vllm_enforce_eager: value === 'on' }
                        : current)}
                    />
                  </FormGroup>
                </>
              )}
            </section>
          ) : null}
          <section className="settings-section">
            <h3>界面</h3>
          <FormGroup label="皮肤">
            <SegmentedControl
              ariaLabel="皮肤"
              value={skin}
              options={[{ label: '浅色', value: 'light' }, { label: '深色', value: 'dark' }]}
              onValueChange={(value) => applySkin(value as Skin)}
            />
          </FormGroup>
          </section>
          {settingsError ? <p className="status-error">{settingsError}</p> : null}
        </form>
      </Modal>

      <Modal
        open={keyDialogOpen}
        title={issuedSecret ? "API Key" : "新建 API Key"}
        closeLabel="关闭 API Key"
        onClose={() => setKeyDialogOpen(false)}
        actions={issuedSecret ? (
          <>
            <Button onClick={() => void navigator.clipboard.writeText(issuedSecret)}>复制</Button>
            <Button onClick={useIssuedKey}>用于当前客户端</Button>
            <Button tone="primary" onClick={() => setKeyDialogOpen(false)}>完成</Button>
          </>
        ) : (
          <>
            <Button onClick={() => setKeyDialogOpen(false)}>取消</Button>
            <Button form="api-key-form" type="submit" tone="primary">创建</Button>
          </>
        )}
      >
        {issuedSecret ? (
          <code className="issued-key">{issuedSecret}</code>
        ) : (
          <form id="api-key-form" className="settings-form" onSubmit={(event) => void issueKey(event)}>
            <FormField label="名称" required>
              <Input required value={keyName} onValueChange={setKeyName} />
            </FormField>
          </form>
        )}
      </Modal>
    </AppShell>
  )
}
