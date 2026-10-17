/**
 * 语音转文字（ASR）设置卡片
 *
 * 两种识别方式：
 * 1. 本地离线模型（默认推荐）：装 faster-whisper 后完全离线，不需要任何 API Key
 * 2. 云端接口：填 OpenAI 兼容服务商的 API Key（阿里云百炼等）
 *
 * provider=auto 时：有云端 Key 走云端，没有就自动用本地模型。
 */
import { useEffect, useState } from 'react'
import { Loader2, Mic, Download } from 'lucide-react'

import {
  ASR_LOCAL_MODEL_HINTS,
  ASR_LOCAL_MODEL_SIZES,
  DEFAULT_ASR_SETTINGS,
  getAsrInstallStatus,
  getAsrStatus,
  installAsrDeps,
  normalizeAsrSettings,
  updateAsrSettings,
  warmupAsrModel,
  type AsrStatus,
} from '@/api/settings'
import { useUIStore } from '@/store/uiStore'
import { getApiErrorMessage } from '@/utils/apiError'
import type { AsrProvider, SystemSettings } from '@/types'

interface AsrSettingsCardProps {
  settings?: Partial<SystemSettings> | null
  onSaved: (next: Partial<SystemSettings>) => void
}

const PROVIDER_LABELS: Record<AsrProvider, string> = {
  auto: '自动（有 Key 走云端，否则本地）',
  local: '本地离线模型（不需要任何 Key）',
  cloud: '云端接口（需要 API Key）',
}

export function AsrSettingsCard({ settings, onSaved }: AsrSettingsCardProps) {
  const { addToast } = useUIStore()
  const normalized = normalizeAsrSettings(settings)
  const [provider, setProvider] = useState<AsrProvider>(normalized['asr.provider'])
  const [localModel, setLocalModel] = useState(normalized['asr.local_model'])
  const [apiKey, setApiKey] = useState(normalized['asr.api_key'])
  const [baseUrl, setBaseUrl] = useState(normalized['asr.base_url'])
  const [model, setModel] = useState(normalized['asr.model'])
  const [publicBase, setPublicBase] = useState(normalized['media.public_base_url'])
  const [saving, setSaving] = useState(false)
  const [showKey, setShowKey] = useState(false)
  const [status, setStatus] = useState<AsrStatus | null>(null)
  const [warming, setWarming] = useState(false)
  // 一键安装本地依赖：installing=进行中，installMsg=进度文案，installLog=pip 输出
  const [installing, setInstalling] = useState(false)
  const [installMsg, setInstallMsg] = useState('')
  const [installLog, setInstallLog] = useState('')
  // 装完后展示「接下来怎么用」的指引
  const [showGuide, setShowGuide] = useState(false)

  useEffect(() => {
    const next = normalizeAsrSettings(settings)
    setProvider(next['asr.provider'])
    setLocalModel(next['asr.local_model'])
    setApiKey(next['asr.api_key'])
    setBaseUrl(next['asr.base_url'])
    setModel(next['asr.model'])
    setPublicBase(next['media.public_base_url'])
  }, [settings])

  useEffect(() => {
    void (async () => {
      try {
        const res = await getAsrStatus()
        if (res.success && res.data) setStatus(res.data)
      } catch {
        // 状态查询失败不影响设置本身
      }
    })()
  }, [])

  const localUsable = provider === 'local' || provider === 'auto'
  const cloudUsable = provider === 'cloud' || provider === 'auto'

  const handleSave = async () => {
    if (saving) return
    const payload: Partial<SystemSettings> = {
      'asr.provider': provider,
      'asr.local_model': localModel.trim() || DEFAULT_ASR_SETTINGS['asr.local_model'],
      'asr.api_key': apiKey.trim(),
      'asr.base_url': baseUrl.trim() || DEFAULT_ASR_SETTINGS['asr.base_url'],
      'asr.model': model.trim() || DEFAULT_ASR_SETTINGS['asr.model'],
      'media.public_base_url': publicBase.trim(),
    }
    try {
      setSaving(true)
      const result = await updateAsrSettings(payload)
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '语音转文字设置保存失败' })
        return
      }
      onSaved(payload)
      addToast({ type: 'success', message: '语音设置已保存，聊天页立即可用' })
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '语音转文字设置保存失败') })
    } finally {
      setSaving(false)
    }
  }

  const handleWarmup = async () => {
    if (warming) return
    try {
      setWarming(true)
      addToast({ type: 'info', message: '正在下载并加载模型，首次较慢，请稍候…' })
      const result = await warmupAsrModel(localModel)
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '模型下载失败' })
        return
      }
      addToast({ type: 'success', message: result.message || '模型已就绪' })
      const res = await getAsrStatus()
      if (res.success && res.data) setStatus(res.data)
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '模型下载失败') })
    } finally {
      setWarming(false)
    }
  }

  /** 一键安装本地识别依赖（[asr]：faster-whisper + av==14.0.1） */
  const handleInstallDeps = async () => {
    if (installing) return
    try {
      setInstalling(true)
      setInstallLog('')
      setInstallMsg('正在安装依赖（首次会下载 torch，约几百 MB，可能需要几分钟）…')

      const start = await installAsrDeps()
      if (!start.success) {
        addToast({ type: 'error', message: start.message || '启动安装失败' })
        setInstalling(false)
        return
      }
      // 本来就已经装好了（例如手动装过）：直接给指引，不用再装一遍
      if (start.data?.already) {
        setInstallMsg('本地识别依赖已安装，无需重复安装')
        setShowGuide(true)
        const res = await getAsrStatus()
        if (res.success && res.data) setStatus(res.data)
        addToast({ type: 'success', message: start.message || '本地识别依赖已安装' })
        setInstalling(false)
        return
      }

      // 后台安装中：轮询进度，直到 done
      const poll = async (): Promise<void> => {
        try {
          const s = await getAsrInstallStatus()
          const d = s.data
          if (!d) return
          setInstallMsg(d.message || '')
          setInstallLog(d.log || '')
          if (d.done && !d.running) {
            const fresh = await getAsrStatus()
            if (fresh.success && fresh.data) setStatus(fresh.data)
            if (d.success) {
              addToast({ type: 'success', message: d.message || '依赖安装完成' })
              setShowGuide(true)
            } else {
              addToast({ type: 'error', message: d.message || '安装失败' })
            }
            setInstalling(false)
            return
          }
        } catch {
          // 单次轮询失败不打断，继续等下一轮
        }
        window.setTimeout(poll, 2000)
      }
      window.setTimeout(poll, 1500)
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '安装失败') })
      setInstalling(false)
    }
  }

  return (
    <div className="border-t border-slate-100 py-3 dark:border-slate-700">
      <div className="flex items-center gap-2">
        <Mic className="h-4 w-4 text-[rgb(var(--color-primary))]" />
        <div className="min-w-0">
          <p className="font-medium text-slate-900 dark:text-slate-100">语音（转文字 / 发送）</p>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            在线聊天里把说的语音转成文字发出去；默认走本地模型，不需要任何 Key
          </p>
        </div>
      </div>

      <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50/80 p-4 dark:border-slate-700 dark:bg-slate-800/50">
        {/* 识别方式 */}
        <div className="input-group">
          <label className="input-label">识别方式</label>
          <select
            value={provider}
            disabled={saving}
            onChange={(event) => setProvider(event.target.value as AsrProvider)}
            className="input-ios"
          >
            {(Object.keys(PROVIDER_LABELS) as AsrProvider[]).map((key) => (
              <option key={key} value={key}>
                {PROVIDER_LABELS[key]}
              </option>
            ))}
          </select>
        </div>

        {/* 本地离线模型 */}
        {localUsable && (
          <div className="mt-4 rounded-md border border-slate-200 bg-white px-3 py-3 dark:border-slate-600 dark:bg-slate-800">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
              <div className="input-group">
                <label className="input-label">本地模型大小</label>
                <select
                  value={localModel}
                  disabled={saving || warming}
                  onChange={(event) => setLocalModel(event.target.value)}
                  className="input-ios"
                >
                  {ASR_LOCAL_MODEL_SIZES.map((size) => (
                    <option key={size} value={size}>
                      {size} — {ASR_LOCAL_MODEL_HINTS[size]}
                    </option>
                  ))}
                </select>
              </div>
              <button
                type="button"
                disabled={saving || warming}
                onClick={() => void handleWarmup()}
                className="btn-ios-secondary"
              >
                {warming ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                {warming ? '下载中…' : '下载模型'}
              </button>
            </div>

            <div className="mt-2 text-xs text-slate-500 dark:text-slate-400">
              {status === null ? (
                '正在查询本地识别能力…'
              ) : status.localAvailable ? (
                <>
                  本地识别已可用
                  {status.localModelReady ? '（模型已加载，可直接转写）' : '（点「下载模型」提前准备，首次转写也会自动下载）'}
                </>
              ) : (
                <div className="space-y-2">
                  <div>
                    未安装本地识别依赖。可以点下面按钮一键安装（自动装
                    <code className="mx-1 rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-700">
                      faster-whisper
                    </code>
                    和
                    <code className="mx-1 rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-700">
                      av==14.0.1
                    </code>
                    ，版本已锁好不会冲突），或手动在后端目录执行：
                    <code className="mx-1 rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-700">
                      {status.installCommand || 'pip install -e ".[asr]"'}
                    </code>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      disabled={installing}
                      onClick={() => void handleInstallDeps()}
                      className="btn-ios-secondary"
                    >
                      {installing ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Download className="h-4 w-4" />
                      )}
                      {installing ? '安装中…' : '一键安装依赖'}
                    </button>
                    {installing && <span>别关页面，装完会自动提示</span>}
                  </div>
                  {installMsg && <p className="text-slate-600 dark:text-slate-300">{installMsg}</p>}
                  {installLog && (
                    <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded bg-slate-100 p-2 text-[11px] leading-relaxed text-slate-600 dark:bg-slate-900 dark:text-slate-300">
                      {installLog.slice(-1500)}
                    </pre>
                  )}
                </div>
              )}
            </div>

            {/* 依赖装好之后的下一步指引 */}
            {showGuide && (
              <div className="mt-3 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-900 dark:border-emerald-700 dark:bg-emerald-900/20 dark:text-emerald-200">
                <p className="font-medium">依赖已装好，接下来三步就能用：</p>
                <ol className="mt-1.5 list-decimal space-y-1 pl-4">
                  <li>
                    点上面的「下载模型」先把模型准备好（也可以不点，首次转写时会自动下载，需联网）
                  </li>
                  <li>「识别方式」选「本地离线模型」或「自动」，然后点「保存语音设置」</li>
                  <li>回到聊天页，点输入框旁的麦克风说话，说完自动转成文字发出去</li>
                </ol>
                <p className="mt-1.5">
                  依赖已自动生效，<b>不需要重启后端</b>；若页面仍提示未安装，重启一次后端即可。
                </p>
              </div>
            )}
          </div>
        )}

        {/* 云端接口 */}
        {cloudUsable && (
          <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="input-group lg:col-span-2">
              <label className="input-label">
                云端 API Key{provider === 'auto' ? '（选填，不填就用本地模型）' : ''}
              </label>
              <div className="flex items-center gap-2">
                <input
                  type={showKey ? 'text' : 'password'}
                  value={apiKey}
                  disabled={saving}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder="sk-xxxxxxxx（阿里云百炼 API Key）"
                  className="input-ios"
                />
                <button
                  type="button"
                  onClick={() => setShowKey((prev) => !prev)}
                  className="btn-ios-secondary shrink-0"
                >
                  {showKey ? '隐藏' : '显示'}
                </button>
              </div>
            </div>
            <div className="input-group">
              <label className="input-label">云端接口地址</label>
              <input
                type="url"
                value={baseUrl}
                disabled={saving}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder={DEFAULT_ASR_SETTINGS['asr.base_url']}
                className="input-ios"
              />
            </div>
            <div className="input-group">
              <label className="input-label">云端模型名称</label>
              <input
                type="text"
                value={model}
                disabled={saving}
                onChange={(event) => setModel(event.target.value)}
                placeholder={DEFAULT_ASR_SETTINGS['asr.model']}
                className="input-ios"
              />
            </div>
          </div>
        )}

        {/* 发真语音：自建音频托管 */}
        <div className="mt-4 rounded-md border border-slate-200 bg-white px-3 py-3 dark:border-slate-600 dark:bg-slate-800">
          <div className="input-group">
            <label className="input-label">语音消息公网地址（发真语音用，可留空）</label>
            <input
              type="text"
              value={publicBase}
              disabled={saving}
              onChange={(event) => setPublicBase(event.target.value)}
              placeholder="例如 http://1.2.3.4:8089"
              className="input-ios"
            />
          </div>
          <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
            闲鱼网页端的上传通道只收图片（实测 mp3 会被拒、wav 会被图片审核拒），
            所以语音只能由我们自己托管，再把地址放进消息里发给买家。
            <br />
            <b>留空</b>：点「发送语音」会自动转成文字发送（推荐，本地就这么用）。
            <br />
            <b>填服务器外网地址</b>：例如部署到云服务器后填 <code>http://1.2.3.4:8089</code>，
            就能发出买家能点的真语音。
            <br />
            没有公网服务器也想发真语音，可以用内网穿透工具（cpolar / ngrok / frp 等）
            拿到一个公网域名，填在这里。注意对方手机访问不到本机的地址，填了也播不了。
          </p>
        </div>

        <div className="mt-4 flex justify-end">
          <button
            type="button"
            disabled={saving || warming}
            onClick={() => void handleSave()}
            className="btn-ios-primary"
          >
            {saving && <Loader2 className="h-4 w-4 animate-spin" />}
            保存语音设置
          </button>
        </div>
      </div>
    </div>
  )
}
