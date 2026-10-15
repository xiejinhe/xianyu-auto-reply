/**
 * 语音转文字（ASR）设置卡片
 *
 * 用途：给「在线聊天」的语音转文字功能配置接口。
 * 留空 API Key 时会自动回退到账号「AI回复设置」里已配置的接口。
 */
import { useEffect, useState } from 'react'
import { Loader2, Mic } from 'lucide-react'

import { DEFAULT_ASR_SETTINGS, normalizeAsrSettings, updateAsrSettings } from '@/api/settings'
import { useUIStore } from '@/store/uiStore'
import { getApiErrorMessage } from '@/utils/apiError'
import type { SystemSettings } from '@/types'

interface AsrSettingsCardProps {
  settings?: Partial<SystemSettings> | null
  onSaved: (next: Partial<SystemSettings>) => void
}

export function AsrSettingsCard({ settings, onSaved }: AsrSettingsCardProps) {
  const { addToast } = useUIStore()
  const normalized = normalizeAsrSettings(settings)
  const [apiKey, setApiKey] = useState(normalized['asr.api_key'])
  const [baseUrl, setBaseUrl] = useState(normalized['asr.base_url'])
  const [model, setModel] = useState(normalized['asr.model'])
  const [saving, setSaving] = useState(false)
  const [showKey, setShowKey] = useState(false)

  useEffect(() => {
    const next = normalizeAsrSettings(settings)
    setApiKey(next['asr.api_key'])
    setBaseUrl(next['asr.base_url'])
    setModel(next['asr.model'])
  }, [settings])

  const handleSave = async () => {
    if (saving) return
    const payload: Partial<SystemSettings> = {
      'asr.api_key': apiKey.trim(),
      'asr.base_url': baseUrl.trim() || DEFAULT_ASR_SETTINGS['asr.base_url'],
      'asr.model': model.trim() || DEFAULT_ASR_SETTINGS['asr.model'],
    }
    try {
      setSaving(true)
      const result = await updateAsrSettings(payload)
      if (!result.success) {
        addToast({ type: 'error', message: result.message || '语音转文字设置保存失败' })
        return
      }
      onSaved(payload)
      addToast({ type: 'success', message: '语音转文字设置已保存，聊天页立即可用' })
    } catch (error) {
      addToast({ type: 'error', message: getApiErrorMessage(error, '语音转文字设置保存失败') })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="border-t border-slate-100 py-3 dark:border-slate-700">
      <div className="flex items-center gap-2">
        <Mic className="h-4 w-4 text-[rgb(var(--color-primary))]" />
        <div className="min-w-0">
          <p className="font-medium text-slate-900 dark:text-slate-100">语音转文字</p>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            在线聊天里把语音转成文字发出去；留空则自动使用账号「AI回复设置」的接口
          </p>
        </div>
      </div>

      <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50/80 p-4 dark:border-slate-700 dark:bg-slate-800/50">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="input-group lg:col-span-2">
            <label className="input-label">API Key</label>
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
            <label className="input-label">接口地址</label>
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
            <label className="input-label">模型名称</label>
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

        <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
          默认使用阿里云百炼（DashScope）的 OpenAI 兼容接口，新用户有免费额度；也可以填其他支持音频转写的
          OpenAI 兼容服务商地址（会尝试 /audio/transcriptions 接口）。
        </p>

        <div className="mt-4 flex justify-end">
          <button
            type="button"
            disabled={saving}
            onClick={() => void handleSave()}
            className="btn-ios-primary"
          >
            {saving && <Loader2 className="h-4 w-4 animate-spin" />}
            保存语音转文字设置
          </button>
        </div>
      </div>
    </div>
  )
}
