export type Sequenced = { seq: number; kind: string };

export function mergeEvents<T extends Sequenced>(existing: T[], incoming: T[]): T[] {
  if (!incoming.length) return existing;
  const last = existing.at(-1)?.seq || 0;
  const next = incoming.filter(event => event.seq > last);
  return next.length ? [...existing, ...next] : existing;
}

export function activeAttempts(events: Array<{ kind: string }>): number {
  return Math.max(0, events.filter(e => e.kind === 'attempt.started').length -
                     events.filter(e => e.kind === 'attempt.finished').length);
}
