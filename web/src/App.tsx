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
import { getStatus, normalizeEndpoint, type ConnectionSettings, type ServiceStatus } from './api'
import { MicrophoneStream, type StreamEvent } from './stream'

type Skin = 'light' | 'dark'

function initialConnection(): ConnectionSettings {
  return {
    endpoint: localStorage.getItem('xgc2-stt.endpoint') || window.location.origin,
    apiKey: localStorage.getItem('xgc2-stt.apiKey') || '',
    outputScript: localStorage.getItem('xgc2-stt.outputScript') === 'original' ? 'original' : 'simplified',
    trimLeadingSilence: localStorage.getItem('xgc2-stt.trimLeadingSilence') !== 'false',
  }
}

export default function App() {
  const [connection, setConnection] = useState<ConnectionSettings>(initialConnection)
  const [draftConnection, setDraftConnection] = useState<ConnectionSettings>(initialConnection)
  const [status, setStatus] = useState<ServiceStatus | null>(null)
  const [statusError, setStatusError] = useState('')
  const [captureState, setCaptureState] = useState<AudioCaptureState>('idle')
  const captureStateRef = useRef<AudioCaptureState>('idle')
  const [streamState, setStreamState] = useState('')
  const [partialText, setPartialText] = useState('')
  const [transcript, setTranscript] = useState('')
  const [captureError, setCaptureError] = useState('')
  const [waveformLevels, setWaveformLevels] = useState<readonly number[]>([])
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [skin, setSkinState] = useState<Skin>(() => localStorage.getItem('xgc2-stt.skin') === 'dark' ? 'dark' : 'light')
  const streamRef = useRef<MicrophoneStream | null>(null)
  const acceptStreamEventsRef = useRef(false)
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
    try {
      setStatus(await getStatus(connection))
      setStatusError('')
    } catch (error) {
      setStatusError(error instanceof Error ? error.message : '状态请求失败')
    }
  }, [connection])

  useEffect(() => {
    document.documentElement.dataset.skin = skin
    void refreshStatus()
    const pollTimer = window.setInterval(() => void refreshStatus(), 5000)
    return () => window.clearInterval(pollTimer)
  }, [refreshStatus, skin])

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
      setPartialText(event.text || '')
      setStreamState('转写中')
      return
    }
    if (event.type === 'transcript.final') {
      const normalized = (event.text || '').trim()
      if (normalized) setTranscript((current) => current ? `${current}\n${normalized}` : normalized)
      setPartialText('')
      setStreamState(captureStateRef.current === 'recording' ? '录音中' : '')
      if (captureStateRef.current === 'finalizing') {
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
    setStreamState('')
    setWaveformLevels([])
  }, [transitionCapture])

  const clearTranscript = useCallback(async () => {
    acceptStreamEventsRef.current = false
    if (captureStateRef.current !== 'idle') await streamRef.current?.cancel()
    transitionCapture('idle')
    setTranscript('')
    setPartialText('')
    setCaptureError('')
    setStreamState('')
  }, [transitionCapture])

  const copyTranscript = useCallback(async () => {
    const value = [transcript, partialText].filter(Boolean).join('\n')
    if (value) await navigator.clipboard.writeText(value)
  }, [partialText, transcript])

  const openSettings = () => {
    setDraftConnection(connection)
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
    setSettingsOpen(false)
    try {
      setStatus(await getStatus(next))
      setStatusError('')
    } catch (error) {
      setStatusError(error instanceof Error ? error.message : '状态请求失败')
    }
  }

  const hasTranscript = Boolean(transcript || partialText)

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
            {partialText ? <span className="partial-text">{transcript ? '\n' : ''}{partialText}</span> : null}
          </div>
        </Panel>

        <Panel className="runtime-panel" padding="none" title="运行状态">
          {status ? (
            <dl className="runtime-grid">
              <div><dt>模型</dt><dd>{status.engine.model}</dd></div>
              <div><dt>引擎</dt><dd>{status.engine.variant} · {status.engine.backend}</dd></div>
              <div><dt>推理</dt><dd>{status.engine.device} · {status.engine.compute_type}</dd></div>
              <div><dt>模型延迟档位</dt><dd>{status.stream.transcription_delay_ms} ms</dd></div>
              <div><dt>GPU</dt><dd>{status.engine.cuda_devices ?? '未知'} 个设备</dd></div>
              <div><dt>版本</dt><dd>{status.version}</dd></div>
              <div><dt>认证</dt><dd>{status.authentication}</dd></div>
              <div><dt>服务地址</dt><dd>{connection.endpoint}</dd></div>
            </dl>
          ) : <p className="status-error">{statusError || '读取中'}</p>}
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
            <Button form="stt-settings" type="submit" tone="primary">保存</Button>
          </>
        )}
      >
        <form id="stt-settings" className="settings-form" onSubmit={(event) => void saveSettings(event)}>
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
          <FormGroup label="皮肤">
            <SegmentedControl
              ariaLabel="皮肤"
              value={skin}
              options={[{ label: '浅色', value: 'light' }, { label: '深色', value: 'dark' }]}
              onValueChange={(value) => applySkin(value as Skin)}
            />
          </FormGroup>
        </form>
      </Modal>
    </AppShell>
  )
}
