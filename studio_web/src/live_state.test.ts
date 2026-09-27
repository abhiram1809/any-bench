import { describe, expect, it } from 'vitest';
import { activeAttempts, mergeEvents } from './live_state';

describe('event replay', () => {
  it('keeps one copy of a delivered event after reconnect', () => {
    const first = [{ seq: 1, kind: 'attempt.started' }];
    expect(mergeEvents(first, [{ seq: 1, kind: 'attempt.started' },
      { seq: 2, kind: 'attempt.finished' }]).map(e => e.seq)).toEqual([1, 2]);
  });

  it('does not show a negative active count for an incomplete replay page', () => {
    expect(activeAttempts([{ kind: 'attempt.finished' }])).toBe(0);
  });
});
