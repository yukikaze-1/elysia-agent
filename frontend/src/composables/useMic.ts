import { useAudioStore } from '@/stores/audioStore'

/**
 * Reactive mic (microphone) state accessor.
 *
 * Core mic capture logic lives in InputBar.vue; this composable provides
 * a convenient reactive handle on the activation state for any component
 * that needs to reflect or react to mic state (e.g. a floating mic button).
 */
export function useMic() {
  const audio = useAudioStore()
  return { isMicActive: audio.isMicActive }
}
