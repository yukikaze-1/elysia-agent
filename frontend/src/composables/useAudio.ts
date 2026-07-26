import { useAudioStore } from '@/stores/audioStore'
import { useSettingsStore } from '@/stores/settingsStore'

/**
 * Thin composable wrapping audio playback and settings access.
 *
 * Audio playback logic lives in audioStore; this composable is an extension
 * point for potential future UI-level audio concerns (e.g. per-message
 * volume overrides, audio visualiser state).
 */
export function useAudio() {
  const audio = useAudioStore()
  const settings = useSettingsStore()
  return { audio, settings }
}
