import { describe, expect, it } from "vitest";
import { mockRunEvents } from "../../shared/stage/mockEvents";
import {
  advanceDemoPlayback,
  createDemoPlayback,
  presentationCelebration,
  replayDemoPlayback,
  toggleDemoPlayback,
} from "./demoPlayback";

describe("deterministic demo playback", () => {
  it("advances only in source order, pauses without changing facts, and restarts from an empty cursor", () => {
    let playback = createDemoPlayback(mockRunEvents);
    expect(playback.state.cursor).toBe(0);
    expect(playback.isPlaying).toBe(true);

    playback = advanceDemoPlayback(playback);
    expect(playback.state.cursor).toBe(1);
    expect(playback.nextIndex).toBe(1);

    playback = toggleDemoPlayback(playback);
    const paused = advanceDemoPlayback(playback);
    expect(paused.state).toBe(playback.state);
    expect(paused.nextIndex).toBe(1);

    const replayed = replayDemoPlayback(playback);
    expect(replayed.state.cursor).toBe(0);
    expect(replayed.state.slices).toEqual({});
    expect(replayed.nextIndex).toBe(0);
    expect(replayed.isPlaying).toBe(true);
  });

  it("keeps reducer-level duplicate, old-generation, and snapshot history safeguards intact", () => {
    let playback = createDemoPlayback(mockRunEvents);
    while (playback.isPlaying) playback = advanceDemoPlayback(playback);

    expect(playback.state.celebrations.map((item) => item.key)).toEqual(["slice-a:0"]);
    expect(playback.state.slices["slice-b"].generation).toBe(1);
  });

  it("emits a presentation key only for a newly accepted celebration", () => {
    let playback = createDemoPlayback(mockRunEvents);
    const before = playback.state;
    while (playback.nextIndex < 9) playback = advanceDemoPlayback(playback);
    const after = playback.state;

    expect(presentationCelebration(before, after)).toBe("slice-a:0");
    expect(presentationCelebration(after, after)).toBeNull();
  });
});
