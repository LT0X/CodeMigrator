import { createInitialStageState, reduceStage } from "../../shared/stage/stageReducer";
import type { RunEvent, StageState } from "../../shared/stage/types";

export interface DemoPlayback {
  readonly events: readonly RunEvent[];
  readonly state: StageState;
  readonly nextIndex: number;
  readonly isPlaying: boolean;
  readonly presentationCelebrationKey: string | null;
}

export const createDemoPlayback = (events: readonly RunEvent[]): DemoPlayback => ({
  events,
  state: createInitialStageState(),
  nextIndex: 0,
  isPlaying: true,
  presentationCelebrationKey: null,
});

const newlyCelebrated = (before: StageState, after: StageState): string | null => {
  const known = new Set(before.celebrations.map((item) => item.key));
  return after.celebrations.find((item) => !known.has(item.key))?.key ?? null;
};

export const advanceDemoPlayback = (playback: DemoPlayback): DemoPlayback => {
  if (!playback.isPlaying || playback.nextIndex >= playback.events.length) return playback;
  const state = reduceStage(playback.state, playback.events[playback.nextIndex]);
  const nextIndex = playback.nextIndex + 1;
  return {
    ...playback,
    state,
    nextIndex,
    isPlaying: nextIndex < playback.events.length,
    presentationCelebrationKey: newlyCelebrated(playback.state, state) ?? playback.presentationCelebrationKey,
  };
};

export const toggleDemoPlayback = (playback: DemoPlayback): DemoPlayback => ({
  ...playback,
  isPlaying: playback.nextIndex < playback.events.length ? !playback.isPlaying : playback.isPlaying,
});

export const replayDemoPlayback = (playback: DemoPlayback): DemoPlayback => createDemoPlayback(playback.events);

export const presentationCelebration = (before: StageState, after: StageState): string | null => newlyCelebrated(before, after);
