import { useCallback, useEffect, useMemo, useState } from 'react';
import { ReactFlow, Background, Controls, MiniMap, Position, type Edge, type Node, type NodeProps } from '@xyflow/react';
import { activeAttempts, mergeEvents } from './live_state';

const api = '/api/experimental/v1';
type LiveEvent = { seq: number; at: number; kind: string; stage: string; data: { payload?: Record<string, unknown>; case_id?: string; model?: string; harness?: string; attempt?: number; evaluation?: string; span_id?: string; parent_span_id?: string | null } };
type Run = { id: string; meta: Record<string, unknown>; latest: number };
type RecordRow = { case_id: string; model: string; harness: string; attempt: number; status: string; seconds: number; test_passed: boolean | null; judge_score: number | null; judge_reason: string; prompt_tokens: number; completion_tokens: number; trace: unknown[]; diff: string; error: string; started_at: number; finished_at: number; concurrency: number };
type AttemptSummary = Pick<RecordRow, 'case_id' | 'model' | 'harness' | 'attempt' | 'concurrency' | 'status' | 'test_passed' | 'judge_score'>;
type Problem = { case_id: string; repository: string; problem_statement: string; base_commit: string; target_commit: string; test_command: string; state?: string; reason?: string; in_verified_dataset?: boolean | null; attempts?: AttemptSummary[] };
type Snapshot = { cursor: number; latest: number; has_more: boolean; events: LiveEvent[]; meta: Record<string, unknown>; controls: Array<{ id: string; action: string; status: string; reason: string; value: number | null }>; unresolved_operations: unknown[] };

async function json<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...options });
  if (!response.ok) throw new Error((await response.text()).slice(0, 500));
  return response.json() as Promise<T>;
}
const post = <T,>(path: string, body: unknown) => json<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const formatDuration = (seconds: number) => `${Math.floor(seconds / 3600).toString().padStart(2, '0')}:${Math.floor(seconds / 60 % 60).toString().padStart(2, '0')}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;
const short = (value: string, size = 22) => value.length > size ? value.slice(0, size - 1) + '…' : value;
const pipeline = [
  { id: 'repository', name: 'Repositories', code: '01', detail: 'Git history and frozen revisions' },
  { id: 'builder', name: 'Builder', code: '02', detail: 'Prompt, tools and drafted problems' },
  { id: 'environment', name: 'Environment', code: '03', detail: 'Docker recipe, build and repair' },
  { id: 'validation', name: 'Verification', code: '04', detail: 'Base fails · reference passes' },
  { id: 'queue', name: 'Verified queue', code: '05', detail: 'Only eligible, checked problems' },
  { id: 'candidate', name: 'Candidate harness', code: '06', detail: 'Model, tools and workspace' },
  { id: 'evaluation', name: 'Fresh evaluation', code: '07', detail: 'Authoritative test container' },
  { id: 'judge', name: 'Judge', code: '08', detail: 'Optional patch assessment' },
  { id: 'report', name: 'Report', code: '09', detail: 'Results and comparison' },
];

function StageNode({ data }: NodeProps) {
  const value = data as { code: string; name: string; detail: string; state: string; count: number; selected: boolean };
  return <div className={`stage-node ${value.state} ${value.selected ? 'selected' : ''}`}>
    <div className="node-head"><span className="node-code">{value.code}</span><span className={`status-dot ${value.state}`} /><span className="node-state">{value.state}</span></div>
    <strong>{value.name}</strong><span className="node-detail">{value.detail}</span>
    <div className="node-foot"><span>{value.count} events</span><span aria-hidden="true">↗</span></div>
  </div>;
}
const nodeTypes = { stage: StageNode };

function App() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [runId, setRunId] = useState(new URLSearchParams(location.search).get('run') || '');
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [meta, setMeta] = useState<Record<string, unknown>>({});
  const [controls, setControls] = useState<Snapshot['controls']>([]);
  const [unresolved, setUnresolved] = useState<unknown[]>([]);
  const [connection, setConnection] = useState('offline');
  const [records, setRecords] = useState<RecordRow[]>([]);
  const [detailRecords, setDetailRecords] = useState<RecordRow[]>([]);
  const [recordTotal, setRecordTotal] = useState(0);
  const [problems, setProblems] = useState<Problem[]>([]);
  const [problemTotal, setProblemTotal] = useState(0);
  const [problemOffset, setProblemOffset] = useState(0);
  const [summary, setSummary] = useState<{ groups?: Array<Record<string, number | string | null>> }>({});
  const [selected, setSelected] = useState('candidate');
  const [tab, setTab] = useState('Overview');
  const [history, setHistory] = useState<number | null>(null);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('all');
  const [modelFilter, setModelFilter] = useState('');
  const [harnessFilter, setHarnessFilter] = useState('');
  const [judgeMin, setJudgeMin] = useState('');
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [target, setTarget] = useState(2);
  const [error, setError] = useState('');
  const [tick, setTick] = useState(Date.now());
  const [showLaunch, setShowLaunch] = useState(false);
  const [showAttach, setShowAttach] = useState(false);
  const [attachPath, setAttachPath] = useState('');
  const [repoText, setRepoText] = useState('');
  const [commits, setCommits] = useState(50);
  const [maxProblems, setMaxProblems] = useState(10);
  const [configText, setConfigText] = useState('');
  const [configOpen, setConfigOpen] = useState(false);
  const [advanced, setAdvanced] = useState('');
  const [launchReview, setLaunchReview] = useState<{ workload: Record<string, unknown>; confirmation: string } | null>(null);
  const [artifactText, setArtifactText] = useState('');
  const [artifactTitle, setArtifactTitle] = useState('');
  const [focusedEvent, setFocusedEvent] = useState<LiveEvent | null>(null);

  useEffect(() => { const id = setInterval(() => setTick(Date.now()), 1000); return () => clearInterval(id); }, []);
  const refreshRuns = useCallback(async () => setRuns(await json<Run[]>(`${api}/runs`)), []);
  useEffect(() => { refreshRuns().catch(e => setError(String(e))); const id = setInterval(() => refreshRuns().catch(() => {}), 5000); return () => clearInterval(id); }, [refreshRuns]);
  useEffect(() => {
    if (!runId) return;
    window.history.replaceState(null, '', `/?run=${runId}`);
    setEvents([]); setMeta({}); setRecords([]); setDetailRecords([]); setProblems([]); setProblemOffset(0); setHistory(null); setConnection('connecting');
    let closed = false; let source: EventSource | undefined;
    async function open() {
      let cursor = 0; let state: Snapshot;
      do {
        state = await json<Snapshot>(`${api}/runs/${runId}/snapshot?after=${cursor}&limit=1000`);
        if (closed) return;
        setEvents(old => mergeEvents(old, state.events)); cursor = state.cursor;
      } while (state.has_more);
      setMeta(state.meta); setControls(state.controls); setUnresolved(state.unresolved_operations);
      source = new EventSource(`${api}/runs/${runId}/events?after=${cursor}`);
      source.onopen = () => setConnection('live');
      source.onerror = () => setConnection('reconnecting');
      source.onmessage = message => {
        const event = JSON.parse(message.data) as LiveEvent;
        setEvents(old => mergeEvents(old, [event]));
        if (event.kind.startsWith('control.') || event.kind.startsWith('session.')) json<Snapshot>(`${api}/runs/${runId}/snapshot?after=${event.seq}&limit=1`).then(s => { setMeta(s.meta); setControls(s.controls); }).catch(() => {});
      };
    }
    open().catch(e => setError(String(e)));
    return () => { closed = true; source?.close(); };
  }, [runId]);
  useEffect(() => {
    if (!runId) return;
    const refresh = async () => {
      const [r, p] = await Promise.all([
        json<{ total: number; records: RecordRow[] }>(`${api}/runs/${runId}/records?limit=200`),
        json<{ total: number; problems: Problem[] }>(`${api}/runs/${runId}/problems?offset=${problemOffset}&limit=200&query=${encodeURIComponent(search)}&model=${encodeURIComponent(modelFilter)}&harness=${encodeURIComponent(harnessFilter)}&state=${['verified', 'invalid', 'skipped'].includes(filter) ? filter : ''}&test_result=${['passed', 'failed'].includes(filter) ? filter : ''}${judgeMin ? `&judge_min=${encodeURIComponent(judgeMin)}` : ''}`),
      ]);
      setRecords(r.records); setRecordTotal(r.total); setProblems(p.problems); setProblemTotal(p.total);
      json<typeof summary>(`${api}/runs/${runId}/summary`).then(setSummary).catch(() => {});
    };
    refresh().catch(() => {}); const id = setInterval(() => refresh().catch(() => {}), 3000);
    return () => clearInterval(id);
  }, [runId, problemOffset, search, modelFilter, harnessFilter, filter, judgeMin]);
  const visible = history === null ? events : events.slice(0, history);
  const latest = visible[visible.length - 1];
  const started = Number(meta.started_at || events[0]?.at || tick / 1000);
  const terminal = [...events].reverse().find(e => e.kind === 'session.finished' || e.kind === 'session.stopped' || e.kind === 'session.failed')?.at;
  const timeNow = history === null ? (terminal || tick / 1000) : (visible.at(-1)?.at || started);
  const elapsed = Math.max(0, timeNow - started);
  let pauseStart: number | null = null;
  let pausedSeconds = 0;
  for (const event of visible) {
    if (event.kind !== 'control.applied') continue;
    if (event.data.payload?.action === 'pause') pauseStart = event.at;
    if (event.data.payload?.action === 'resume' && pauseStart !== null) { pausedSeconds += event.at - pauseStart; pauseStart = null; }
  }
  if (pauseStart !== null) pausedSeconds += timeNow - pauseStart;
  const activeTime = Math.max(0, elapsed - pausedSeconds);
  const expectedAttempts = summary.groups?.reduce((sum, group) => sum + Number(group.expected_attempts || 0), 0) || 0;
  const testPasses = summary.groups?.reduce((sum, group) => sum + Number(group.test_passed || 0), 0) || 0;
  const judgeScores = summary.groups?.reduce((sum, group) => sum + Math.round(Number(group.judge_coverage || 0) * Number(group.attempts || 0)), 0) || 0;
  const eta = recordTotal >= 2 && expectedAttempts > recordTotal ? (expectedAttempts - recordTotal) * activeTime / recordTotal : null;
  const stageCounts = useMemo(() => Object.fromEntries(pipeline.map(p => [p.id, visible.filter(e => e.stage === p.id || e.stage === ({ queue: 'validation', report: 'report', repository: 'builder' } as Record<string, string>)[p.id]).length])), [visible]);
  const activeStage = meta.state === 'finished' || meta.state === 'stopped' ? String(meta.state) : ([...visible].reverse().find(e => e.kind === 'stage.started' || e.kind.endsWith('.started'))?.stage || 'repository');
  const stageState = (id: string) => {
    const mapped = id === 'repository' ? 'builder' : id === 'queue' ? 'validation' : id;
    const matches = visible.filter(e => e.stage === mapped);
    if (!matches.length) return 'waiting';
    const last = matches[matches.length - 1];
    if (last.kind.endsWith('.failed') || last.kind.endsWith('.error')) return 'failed';
    if (last.kind.endsWith('.finished') || last.kind.endsWith('.completed')) return 'done';
    return id === activeStage ? 'running' : 'done';
  };
  const selectedRecord = [...detailRecords, ...records].find(r => `${r.case_id}:${r.model}:${r.attempt}` === selected);
  const selectedProblem = problems.find(p => p.case_id === selected);
  const stage = pipeline.find(p => p.id === selected);
  const boardMode = selectedRecord ? 'attempt' : selectedProblem ? 'problem' : 'pipeline';
  const graph = useMemo(() => {
    const caseEvents = selectedRecord || selectedProblem ? visible.filter(e => e.data.case_id === (selectedRecord?.case_id || selectedProblem?.case_id)) : [];
    const attemptEvents = selectedRecord ? caseEvents.filter(e => !e.data.model || e.data.model === selectedRecord.model) : [];
    const descriptors = boardMode === 'pipeline' ? pipeline : boardMode === 'problem' ? [
      { id: 'repository', name: 'Revision', code: '01', detail: short(selectedProblem?.repository || '', 28) },
      { id: 'builder', name: 'Builder', code: '02', detail: 'Problem and recipe' },
      { id: 'validation', name: 'Verification', code: '03', detail: 'Base and reference tests' },
      { id: 'candidate', name: 'Candidate attempts', code: '04', detail: `${records.filter(r => r.case_id === selectedProblem?.case_id).length} saved attempts` },
      { id: 'evaluation', name: 'Final tests', code: '05', detail: 'Authoritative evaluation' },
      { id: 'judge', name: 'Judge', code: '06', detail: 'Runs after candidate work' },
    ] : [
      { id: 'prompt', name: 'Prompt & model', code: '01', detail: `${selectedRecord?.model || ''} · ${selectedRecord?.harness || ''}` },
      { id: 'endpoint', name: 'Model endpoint', code: '02', detail: short(String(attemptEvents.find(e => e.kind === 'model.request')?.data.payload?.endpoint || 'Unavailable'), 28) },
      { id: 'tools', name: 'Tools & files', code: '03', detail: 'Reads, edits, responses' },
      { id: 'container', name: 'Candidate container', code: '04', detail: 'Isolated workspace' },
      { id: 'selfcheck', name: 'Self-checks', code: '05', detail: 'Candidate-reported tests' },
      { id: 'evaluation', name: 'Fresh evaluation', code: '06', detail: 'Authoritative final tests' },
      { id: 'judge', name: 'Judge', code: '07', detail: 'Optional rubric and score' },
    ];
    const matching = (id: string) => boardMode === 'pipeline' ? visible.filter(e => e.stage === id) : boardMode === 'problem' ? caseEvents.filter(e => e.stage === id) : attemptEvents.filter(e =>
      id === 'prompt' ? e.kind.startsWith('model.') : id === 'endpoint' ? e.kind.startsWith('request.') || e.kind === 'model.request' : id === 'tools' ? e.kind.startsWith('tool.') || e.kind === 'harness.event' :
      id === 'container' ? e.kind.startsWith('container.') : id === 'selfcheck' ? e.stage === 'candidate' && (e.kind === 'tool.completed' && e.data.payload?.tool === 'Run' || e.kind === 'harness.event' && e.data.payload?.type === 'test') : e.stage === id);
    const nodes: Node[] = descriptors.map((p, index) => { const row = Math.floor(index / 3); const column = row % 2 === 0 ? index % 3 : 2 - index % 3; const matchingEvents = matching(p.id); const state = boardMode === 'pipeline' ? stageState(p.id) : !matchingEvents.length ? 'waiting' : matchingEvents.at(-1)?.kind.endsWith('.started') ? 'running' : 'done'; return { id: p.id, type: 'stage', width: 224, height: 133, position: positions[`${boardMode}:${p.id}`] || { x: 45 + column * 235, y: 38 + row * 170 }, sourcePosition: row % 2 === 0 ? Position.Right : Position.Left, targetPosition: row % 2 === 0 ? Position.Left : Position.Right, data: { ...p, state, count: boardMode === 'pipeline' ? stageCounts[p.id] || 0 : matchingEvents.length, selected: selected === p.id } }; });
    const edges: Edge[] = descriptors.slice(1).map((p, index) => ({ id: `${descriptors[index].id}-${p.id}`, source: descriptors[index].id, target: p.id, style: { stroke: '#7797a4', strokeWidth: 2 } }));
    return { nodes, edges };
  }, [visible, positions, selected, stageCounts, records, selectedProblem, selectedRecord, boardMode]);
  const related = visible.filter(e => selectedRecord ? e.data.case_id === selectedRecord.case_id && (!e.data.model || e.data.model === selectedRecord.model) : selectedProblem ? e.data.case_id === selectedProblem.case_id : stage ? e.stage === stage.id : true);
  const selectedEvent = focusedEvent && visible.some(e => e.seq === focusedEvent.seq) ? focusedEvent : related[related.length - 1];
  const activeKeys = new Map<string, string>();
  for (const event of visible) {
    if (event.kind !== 'attempt.started' && event.kind !== 'attempt.finished') continue;
    const key = `${event.data.case_id}:${event.data.model}:${event.data.attempt}`;
    if (event.kind === 'attempt.started' && event.data.case_id) activeKeys.set(key, event.data.case_id);
    else activeKeys.delete(key);
  }
  const activeCases = new Set(activeKeys.values());
  const filtered = filter === 'active' ? problems.filter(p => activeCases.has(p.case_id)) : problems;
  const currentTarget = [...visible].reverse().find(e => (e.kind === 'control.applied' && e.data.payload?.action === 'concurrency') || e.kind === 'concurrency.level');
  const scheduling = (meta.concurrency || {}) as { configured?: number; target?: number; effective?: number };
  const requestedTarget = Number(currentTarget?.data.payload?.target || scheduling.target || target);
  const providerCap = scheduling.configured && scheduling.effective ? scheduling.effective : requestedTarget;
  const effectiveTarget = Math.min(requestedTarget, Number(providerCap));
  const draining = requestedTarget < activeAttempts(visible);
  const requests = [...visible].reverse().find(e => ['request.waiting', 'request.admitted', 'request.finished'].includes(e.kind))?.data.payload;
  const runtimeAction = [...visible].reverse().find(e => e.kind === 'control.applied' && ['pause', 'resume', 'stop'].includes(String(e.data.payload?.action)))?.data.payload?.action;
  const livePhase = meta.state === 'running' && runtimeAction === 'stop' ? 'Stopping' : meta.state === 'running' && runtimeAction === 'pause' ? activeAttempts(visible) ? 'Pausing' : 'Paused' : String(meta.state || activeStage);
  const spanStarts = visible.filter(e => e.kind === 'span.started' && e.data.span_id).slice(-18);
  const spanEnds = new Map(visible.filter(e => e.kind === 'span.finished' || e.kind === 'span.failed').map(e => [e.data.span_id, e]));
  const concurrencyEvents = visible.filter(e => e.kind === 'concurrency.level' || e.kind === 'control.applied' && e.data.payload?.action === 'concurrency').slice(-8);
  const containerResource = [...visible].reverse().find(e => e.kind === 'resource.sample' && e.stage === 'docker')?.data.payload;
  const hostGpu = [...visible].reverse().find(e => e.kind === 'resource.sample' && e.stage === 'host_gpu')?.data.payload;
  const dockerStats = (containerResource?.reported || {}) as { CPUPerc?: string; MemUsage?: string };

  async function control(action: string, value?: number) {
    if (!runId) return;
    try { const reply = await post<{ status: string }>(`${api}/runs/${runId}/controls`, { action, value: value ?? null, request_id: crypto.randomUUID() }); setError(`Control ${action}: ${reply.status}`); }
    catch (e) { setError(String(e)); }
  }
  async function resumeRun() {
    try { await post(`${api}/runs/${runId}/resume`, {}); setError('Resuming the saved session.'); }
    catch (e) { setError(String(e)); }
  }
  async function attachRun() {
    try { const result = await post<{ id: string }>(`${api}/runs/attach`, { path: attachPath }); setRunId(result.id); setShowAttach(false); refreshRuns(); }
    catch (e) { setError(String(e)); }
  }
  async function openArtifact(ref: unknown, title: string) {
    if (!runId || !ref || typeof ref !== 'object' || !('id' in ref)) return;
    const response = await fetch(`${api}/runs/${runId}/artifact/${String((ref as { id: string }).id)}`);
    if (response.ok) { setArtifactTitle(title); setArtifactText(await response.text()); }
  }
  async function launch() {
    try {
      const body = advanced.trim() ? { mode: 'advanced', argv: JSON.parse(advanced) } : { mode: 'guided', repositories: repoText.split('\n').map(x => x.trim()).filter(Boolean), commits, max_problems: maxProblems };
      if (!launchReview) { setLaunchReview(await post(`${api}/runs/validate`, body)); return; }
      const run = await post<{ id: string }>(`${api}/runs`, { ...body, confirmed: true });
      setRunId(run.id); setShowLaunch(false); refreshRuns();
    } catch (e) { setError(String(e)); }
  }
  async function saveConfig() {
    try {
      await json(`${api}/config`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: configText });
      setConfigOpen(false); setLaunchReview(null); setError('Configuration saved.');
    } catch (e) { setError(String(e)); }
  }
  async function importFile(file: File) {
    try {
      const format = file.name.toLowerCase().endsWith('.toml') ? 'toml' : 'json';
      const result = await post<{ config?: unknown; experiment_path?: string; command?: string; model_path?: string }>(`${api}/config/import`, { text: await file.text(), format });
      if (result.config) { setConfigText(JSON.stringify(result.config, null, 2)); setConfigOpen(true); }
      if (result.experiment_path) { setAdvanced(JSON.stringify([result.command, '--config', result.experiment_path], null, 2)); setLaunchReview(null); setShowLaunch(true); }
      if (result.model_path) setError(`Model configuration imported at ${result.model_path}. Use it with --models in an advanced run.`);
    } catch (e) { setError(String(e)); }
  }

  return <div className="studio-shell">
    <header className="topbar"><div className="brand"><div className="brand-mark">A<span>◦</span></div><div><strong>AnyBench <em>Studio</em></strong><small>EXPERIMENTAL · LOCAL WORKSPACE</small></div></div>
      <div className="top-actions"><span className={`connection ${connection}`}>● {connection}</span><button onClick={async () => { const config = await json<Record<string, unknown>>(`${api}/config`); setConfigText(JSON.stringify(config, null, 2)); setConfigOpen(true); }}>Configure</button><button className="primary" onClick={() => setShowLaunch(true)}>+ New benchmark</button></div></header>
    <div className="layout"><aside className="sidebar"><div className="side-title">RUNS <button onClick={() => refreshRuns()}>↻</button></div><div className="run-list">{runs.map(r => <button key={r.id} className={`run-item ${r.id === runId ? 'active' : ''}`} onClick={() => setRunId(r.id)}><span>{String(r.meta.command || 'Attached run')}</span><small>{r.id.slice(0, 8)} · {String(r.meta.state || 'running')}</small></button>)}</div><button onClick={() => setShowAttach(true)}>Attach saved run</button><div className="side-help"><b>What you see</b><p>Every stage records what AnyBench actually observed. Open a node, problem or attempt to inspect its evidence.</p><p>Secrets from configured model credentials are redacted.</p></div></aside>
      <main className="main">{!runId ? <div className="empty"><span className="empty-icon">◇</span><h1>Benchmarks, as they happen.</h1><p>Launch a run or attach an existing one. The whiteboard reveals each step from repository history to the final report.</p><button className="primary" onClick={() => setShowLaunch(true)}>Start a benchmark</button></div> : <>
        <div className="run-header"><div><span className="eyebrow">LIVE BENCHMARK · {runId.slice(0, 12)}</span><h1>{String(meta.command || 'Benchmark')} <span className="phase-pill">{livePhase}</span></h1><p>{String(meta.session_path || meta.output_path || meta.verification_path || 'Awaiting output paths')}</p></div><div className="clock"><small>ELAPSED</small><strong>{formatDuration(elapsed)}</strong><span>{recordTotal} attempts · {problemTotal} problems</span></div></div>
        <div className="metrics-strip"><div><small>CURRENT PHASE</small><b>{activeStage}</b></div><div><small>ACTIVE TIME</small><b>{formatDuration(activeTime)}</b></div><div><small>PROVISIONAL ETA</small><b>{eta === null ? '—' : formatDuration(eta)}</b></div><div><small>ACTIVE ATTEMPTS</small><b>{activeAttempts(visible)}</b></div><div><small>TEST PASSES</small><b>{testPasses}</b></div><div><small>JUDGE SCORES</small><b>{judgeScores}</b></div><div><small>CONCURRENCY</small><b>{requestedTarget} requested · {effectiveTarget} effective</b><small>{draining ? 'DRAINING · ' : ''}{requests ? `${String(requests.active || 0)} API active · ${String(requests.waiting || 0)} waiting` : 'Requests unavailable'}</small></div><div><small>DOCKER CPU / RAM</small><b>{dockerStats.CPUPerc || 'Unavailable'}</b><small>{dockerStats.MemUsage || 'Telemetry unavailable'}</small></div><div><small>HOST NVIDIA GPU</small><b>{hostGpu?.reported ? String(hostGpu.reported).split('\n')[0] : 'Unavailable'}</b><small>VRAM used/total · utilization</small></div></div>
        <div className="control-row"><div className="control-label"><strong>Runtime controls</strong><span>{meta.state === 'finished' ? 'This run is complete.' : 'Changes apply at safe work boundaries.'}</span></div><div className="control-buttons">{!meta.read_only && (meta.state === 'stopped' || meta.state === 'failed' || meta.state === 'interrupted') ? <button className="primary" onClick={resumeRun}>Resume saved session</button> : !meta.read_only && meta.state !== 'finished' && <><label>Concurrency <input aria-label="Concurrency" type="number" min="1" max="64" value={target} onChange={e => setTarget(Number(e.target.value))} /></label><button onClick={() => control('concurrency', target)}>Apply</button><button onClick={() => control('pause')}>Pause</button><button onClick={() => control('resume')}>Resume</button><button className="danger" onClick={() => control('stop')}>Graceful stop</button></>}</div></div>
        <section className="workspace-grid"><div className="whiteboard panel"><div className="panel-heading"><div><span className="eyebrow">EXECUTION MAP</span><h2>Pipeline whiteboard</h2><nav className="board-crumbs" aria-label="Whiteboard level"><button onClick={() => { setSelected('candidate'); setFocusedEvent(null); }}>Pipeline</button>{(selectedProblem || selectedRecord) && <> › <button onClick={() => { setSelected((selectedProblem || selectedRecord)?.case_id || 'candidate'); setFocusedEvent(null); }}>Problem {short((selectedProblem || selectedRecord)?.case_id || '', 14)}</button></>}{selectedRecord && <> › <span>Attempt #{selectedRecord.attempt}</span></>}</nav></div><div className="legend"><span><i className="running" /> Running</span><span><i className="done" /> Recorded</span><span><i className="waiting" /> Waiting</span></div></div><div className="flow-wrap"><ReactFlow key={boardMode + ':' + (selectedProblem?.case_id || selectedRecord?.case_id || '')} nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.15 }} minZoom={0.25} maxZoom={1.5} onNodeClick={(_, node) => { if (boardMode === 'pipeline') { setSelected(node.id); setFocusedEvent(null); } else setFocusedEvent([...visible].reverse().find(e => e.data.case_id === (selectedProblem?.case_id || selectedRecord?.case_id) && (e.stage === node.id || node.id === 'prompt' && e.kind.startsWith('model.'))) || null); }} onNodeDragStop={(_, node) => setPositions(old => ({ ...old, [`${boardMode}:${node.id}`]: node.position }))} nodesConnectable={false} deleteKeyCode={null}><Background color="#c9d9df" gap={22} size={1} /><Controls /><MiniMap nodeColor={node => String(node.data.state) === 'running' ? '#e09b50' : String(node.data.state) === 'done' ? '#21878b' : '#bfd0d5'} /></ReactFlow></div></div>
        <section className="inspector panel"><div className="panel-heading"><div><span className="eyebrow">INSPECTOR</span><h2>{stage?.name || selectedProblem?.case_id || selectedRecord?.model || selected}</h2></div><span className="count-pill">{related.length} events</span></div><div className="tabs">{['Overview', 'Prompt/Response', 'Tools', 'Files/Patch', 'Tests', 'Logs', 'Timing'].map(t => <button key={t} className={tab === t ? 'active' : ''} onClick={() => setTab(t)}>{t}</button>)}</div><div className="inspector-body">{tab === 'Overview' && <><p className="muted">{stage?.detail || selectedProblem?.problem_statement || selectedRecord?.status || 'Select a stage, problem or attempt.'}</p>{selectedRecord && <dl><dt>Harness</dt><dd>{selectedRecord.harness}</dd><dt>Local test</dt><dd>{selectedRecord.test_passed === null ? 'Unscored' : selectedRecord.test_passed ? 'Passed' : 'Failed'}</dd><dt>Judge</dt><dd>{selectedRecord.judge_score ?? 'Waiting or unavailable'}</dd><dt>Elapsed</dt><dd>{selectedRecord.seconds.toFixed(1)}s</dd></dl>}{selectedProblem && <dl><dt>Repository</dt><dd>{selectedProblem.repository}</dd><dt>State</dt><dd>{selectedProblem.state || "pending"}</dd><dt>Reason</dt><dd>{selectedProblem.reason || "—"}</dd><dt>Verified dataset</dt><dd>{selectedProblem.in_verified_dataset == null ? "Unavailable" : selectedProblem.in_verified_dataset ? "Included" : "Excluded"}</dd><dt>Base</dt><dd>{selectedProblem.base_commit.slice(0, 12)}</dd><dt>Test</dt><dd>{selectedProblem.test_command || 'Unavailable'}</dd></dl>}<div className="recent-events">{related.slice(-12).reverse().map(e => <button key={e.seq} onClick={() => setFocusedEvent(e)}><time>{new Date(e.at * 1000).toLocaleTimeString()}</time><span>{e.kind}</span></button>)}</div></>}
        {tab === 'Prompt/Response' && <div className="event-list">{related.filter(e => e.kind.startsWith('model.')).slice(-40).reverse().map(e => <div className="event-card" key={e.seq}><small>{new Date(e.at * 1000).toLocaleTimeString()} · {e.kind}</small><b>{String(e.data.payload?.model || e.data.model || '')}</b><p>{JSON.stringify(Object.fromEntries(Object.entries(e.data.payload || {}).filter(([k]) => !['prompt', 'response'].includes(k))))}</p>{['prompt', 'response'].map(k => Boolean(e.data.payload?.[k]) && <button key={k} onClick={() => openArtifact(e.data.payload?.[k], `${k} · ${e.kind}`)}>Open {k}</button>)}</div>)}</div>}
        {tab === 'Tools' && <div className="event-list">{related.filter(e => e.kind === 'tool.completed' || e.kind === 'harness.event').slice(-80).reverse().map(e => <div className="event-card" key={e.seq}><small>{new Date(e.at * 1000).toLocaleTimeString()}</small><b>{String(e.data.payload?.tool || e.kind)}</b><pre>{JSON.stringify(e.data.payload, null, 2)}</pre></div>)}</div>}
        {tab === 'Files/Patch' && <><p className="muted">Changes appear after the attempt finishes.</p><pre>{selectedRecord?.diff || 'No saved patch for this selection.'}</pre></>}
        {tab === 'Tests' && <div className="event-list">{related.filter(e => e.kind.startsWith('test.') || e.kind.startsWith('validation.')).slice(-80).reverse().map(e => <div className="event-card" key={e.seq}><small>{e.kind} · {new Date(e.at * 1000).toLocaleTimeString()}</small><pre>{JSON.stringify(e.data.payload, null, 2)}</pre></div>)}</div>}
        {tab === 'Logs' && <pre className="log-view">{related.filter(e => e.kind.startsWith('process.output')).slice(-500).map(e => e.kind === 'process.output.truncated' ? `\n[Output truncated at ${String(e.data.payload?.limit || 'configured')} characters]\n` : String(e.data.payload?.text || '')).join('') || selectedRecord?.error || 'No live output yet.'}</pre>}
        {tab === 'Timing' && <><dl><dt>Elapsed session time</dt><dd>{formatDuration(elapsed)}</dd><dt>Attempt time</dt><dd>{selectedRecord ? `${selectedRecord.seconds.toFixed(2)}s` : 'Select an attempt'}</dd><dt>Events</dt><dd>{related.length}</dd><dt>Recorded operations needing review</dt><dd>{unresolved.length}</dd></dl><div className="event-list">{related.slice(-30).reverse().map(e => <div className="time-line" key={e.seq}><time>{new Date(e.at * 1000).toLocaleTimeString()}</time><span>{e.kind}</span></div>)}</div></>}
        </div></section></section>
        <section className="timeline panel"><div className="panel-heading"><div><span className="eyebrow">EVENT HISTORY</span><h2>Timeline <span className="light">{history === null ? '· following live' : `· event ${history} of ${events.length}`}</span></h2></div>{history !== null && <button onClick={() => setHistory(null)}>Return to live ↗</button>}</div><input aria-label="Replay event history" type="range" min="0" max={events.length} value={history ?? events.length} onChange={e => setHistory(Number(e.target.value))} /><div className="timeline-marks">{events.slice(-8).map(e => <button key={e.seq} onClick={() => { setHistory(events.findIndex(x => x.seq === e.seq) + 1); setSelected(e.stage); setFocusedEvent(e); }}><span>{new Date(e.at * 1000).toLocaleTimeString()}</span>{short(e.kind, 18)}</button>)}</div><div className="span-history"><div><b>Execution spans</b>{spanStarts.length ? spanStarts.map(e => { const ended = spanEnds.get(e.data.span_id); return <button key={e.seq} onClick={() => { setHistory(events.findIndex(item => item.seq === e.seq) + 1); setFocusedEvent(e); }} style={{ paddingLeft: e.data.parent_span_id ? 24 : 8 }}><span>{e.stage}</span><small>{ended ? `${Number(ended.data.payload?.seconds || 0).toFixed(2)}s` : 'running'}</small></button>; }) : <small>Spans appear as work starts.</small>}</div><div><b>Concurrency history</b>{concurrencyEvents.length ? concurrencyEvents.map(e => <p key={e.seq}>{new Date(e.at * 1000).toLocaleTimeString()} · {String(e.data.payload?.model || 'candidate')} · target {String(e.data.payload?.target || '—')}</p>) : <small>Fixed at the configured limit.</small>}</div></div></section>
        <section className="bottom-grid"><div className="panel problems"><div className="panel-heading"><div><span className="eyebrow">WORK QUEUE</span><h2>Problems <span className="light">{problemTotal}</span></h2></div><div className="table-controls"><input placeholder="Search problems or repository" value={search} onChange={e => { setSearch(e.target.value); setProblemOffset(0); }} /><input placeholder="Model" aria-label="Filter model" value={modelFilter} onChange={e => { setModelFilter(e.target.value); setProblemOffset(0); }} /><input placeholder="Harness" aria-label="Filter harness" value={harnessFilter} onChange={e => { setHarnessFilter(e.target.value); setProblemOffset(0); }} /><input type="number" min="0" max="1" step="0.05" placeholder="Judge ≥" aria-label="Minimum judge score" value={judgeMin} onChange={e => { setJudgeMin(e.target.value); setProblemOffset(0); }} /><select value={filter} onChange={e => { setFilter(e.target.value); setProblemOffset(0); }}><option value="all">All states</option><option value="verified">Verified</option><option value="invalid">Invalid</option><option value="skipped">Skipped</option><option value="active">Active</option><option value="passed">Passed</option><option value="failed">Failed</option></select></div></div><div className="table-scroll"><table><thead><tr><th>Problem</th><th>Repository</th><th>State</th><th>Harness / model</th><th>Test</th><th>Judge</th></tr></thead><tbody>{filtered.map(p => { const attempts = p.attempts || records.filter(r => r.case_id === p.case_id); return <tr key={p.case_id} onClick={() => { setSelected(p.case_id); setFocusedEvent(null); }} tabIndex={0} onKeyDown={e => { if (e.key === 'Enter') setSelected(p.case_id); }}><td><b>{p.case_id}</b><small>{short(p.problem_statement, 72)}</small></td><td>{short(p.repository, 24)}</td><td>{p.state || "pending"}</td><td>{attempts.length ? attempts.map(r => <button key={`${r.model}-${r.attempt}`} onClick={e => { e.stopPropagation(); setSelected(`${r.case_id}:${r.model}:${r.attempt}`); json<{ records: RecordRow[] }>(`${api}/runs/${runId}/records?case_id=${encodeURIComponent(r.case_id)}&limit=200`).then(result => setDetailRecords(result.records)).catch(() => {}); }}>{r.harness} / {r.model} #{r.attempt}</button>) : 'Queued'}</td><td>{attempts.map(r => <span className={`result ${r.test_passed ? 'passed' : r.test_passed === false ? 'failed' : ''}`} key={r.model}>{r.test_passed === null ? '—' : r.test_passed ? 'Pass' : 'Fail'}</span>)}</td><td>{attempts.map(r => <span key={r.model}>{r.judge_score === null ? '—' : r.judge_score.toFixed(2)}</span>)}</td></tr>; })}</tbody></table></div><div className="page-actions"><span>{problemTotal ? problemOffset + 1 : 0}–{Math.min(problemOffset + problems.length, problemTotal)} of {problemTotal}</span><button disabled={problemOffset === 0} onClick={() => setProblemOffset(Math.max(0, problemOffset - 200))}>Previous</button><button disabled={problemOffset + problems.length >= problemTotal} onClick={() => setProblemOffset(problemOffset + 200)}>Next</button></div></div>
        <div className="panel comparison"><div className="panel-heading"><div><span className="eyebrow">RESULTS</span><h2>Model comparison</h2></div></div><div className="comparison-body">{(summary.groups || []).map((g, i) => <div className="model-card" key={i}><b>{String(g.model)} <small>{String(g.harness)} · {g.variable_concurrency ? 'variable concurrency' : `${g.concurrency} workers`}</small></b><div><span>Attempts <strong>{String(g.attempts)}</strong></span><span>Verified solve rate <strong>{g.verified_test_accuracy === null ? '—' : `${Math.round(Number(g.verified_test_accuracy) * 100)}%`}</strong></span><span>Judge coverage <strong>{g.judge_coverage === null ? '—' : `${Math.round(Number(g.judge_coverage) * 100)}%`}</strong></span><span>Throughput <strong>{Math.round(Number(g.throughput))}/h</strong></span><span>Latency p50 <strong>{g.latency_p50 === null ? '—' : `${Number(g.latency_p50).toFixed(1)}s`}</strong></span><span>Errors <strong>{String(g.errors || 0)}</strong></span><span>Tokens <strong>{Number(g.prompt_tokens || 0) + Number(g.completion_tokens || 0) || '—'}</strong></span><span>Cost <strong>{g.estimated_cost === null ? '—' : `$${Number(g.estimated_cost).toFixed(4)}`}</strong></span></div></div>)}{!summary.groups?.length && <p className="muted">Results appear as attempts finish.</p>}</div></div></section>
        {controls.length > 0 && <div className="control-history">Latest control: {controls[0].action} · {controls[0].status}{controls[0].reason ? ` · ${controls[0].reason}` : ''}</div>}
      </>}</main></div>
    {error && <div role="status" className="toast"><span>{error}</span><button onClick={() => setError('')}>×</button></div>}
    {artifactText && <div className="modal-backdrop" onClick={() => setArtifactText('')}><div className="modal artifact-modal" onClick={e => e.stopPropagation()}><div className="modal-heading"><h2>{artifactTitle}</h2><button onClick={() => setArtifactText('')}>Close</button></div><pre>{artifactText}</pre></div></div>}
    {showAttach && <div className="modal-backdrop" onClick={() => setShowAttach(false)}><div className="modal" onClick={e => e.stopPropagation()}><div className="modal-heading"><h2>Attach saved run</h2><button onClick={() => setShowAttach(false)}>Close</button></div><p>Open a session directory or result JSONL on this machine.</p><label>Absolute path<input value={attachPath} onChange={e => setAttachPath(e.target.value)} placeholder="/path/to/.anybench/runs/session" /></label><div className="modal-actions"><button className="primary" onClick={attachRun}>Attach</button></div></div></div>}
    {configOpen && <div className="modal-backdrop" onClick={() => setConfigOpen(false)}><div className="modal" onClick={e => e.stopPropagation()}><div className="modal-heading"><div><span className="eyebrow">CONFIGURATION</span><h2>Model and repository setup</h2></div><button onClick={() => setConfigOpen(false)}>Close</button></div><p>Edit the existing AnyBench JSON configuration. All model and harness settings are available.</p><label>Import AnyBench JSON or TOML<input type="file" accept=".json,.toml" onChange={e => { const file = e.target.files?.[0]; if (file) importFile(file); }} /></label><textarea className="config-editor" value={configText} onChange={e => setConfigText(e.target.value)} /><div className="modal-actions"><button className="primary" onClick={saveConfig}>Save configuration</button></div></div></div>}
    {showLaunch && <div className="modal-backdrop" onClick={() => setShowLaunch(false)}><div className="modal" onClick={e => e.stopPropagation()}><div className="modal-heading"><div><span className="eyebrow">NEW RUN</span><h2>Launch a benchmark</h2></div><button onClick={() => setShowLaunch(false)}>Close</button></div><label>Repositories, one path or Git URL per line<textarea value={repoText} onChange={e => { setRepoText(e.target.value); setLaunchReview(null); }} placeholder="/path/to/repository" /></label><div className="form-row"><label>Commits per repository<input type="number" min="1" value={commits} onChange={e => { setCommits(Number(e.target.value)); setLaunchReview(null); }} /></label><label>Maximum verified problems<input type="number" min="1" value={maxProblems} onChange={e => { setMaxProblems(Number(e.target.value)); setLaunchReview(null); }} /></label></div><details><summary>Advanced CLI command</summary><p>Provide the exact argument array for build, validate, run, or evaluate. Existing configuration files and flags remain available.</p><label>Import command TOML<input type="file" accept=".toml" onChange={e => { const file = e.target.files?.[0]; if (file) importFile(file); }} /></label><textarea value={advanced} onChange={e => { setAdvanced(e.target.value); setLaunchReview(null); }} placeholder={'["run", ".anybench/cases.csv", "--models", ".anybench/candidates.json", "--output", ".anybench/attempts.jsonl"]'} /></details><div className="launch-estimate">{launchReview ? <><b>Validated workload</b><pre>{JSON.stringify(launchReview.workload, null, 2)}</pre><span>{launchReview.confirmation}</span></> : <>Review the workload before confirming provider calls.</>}</div><div className="modal-actions"><button className="primary" onClick={launch}>{launchReview ? "Confirm and launch" : "Review workload"}</button></div></div></div>}
  </div>;
}
export default App;
