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
  getAsrStatus,
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
                <>
                  未安装本地识别依赖，请在后端目录执行：
                  <code className="mx-1 rounded bg-slate-100 px-1 py-0.5 dark:bg-slate-700">
                    {status.installCommand || 'pip install faster-whisper'}
                  </code>
                  后重启后端
                </>
              )}
            </div>
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
