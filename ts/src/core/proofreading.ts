// Proofreading Mode (translation.anthropic.advisor.*) gate for the console.
//
// Mirrors client.py::_validate_advisor_config so the console says at save time
// what the backend will say at run time:
//  - an executor/advisor pair outside ADVISOR_COMPATIBILITY is a WARNING. It is
//    saved, the mode stays on, and the Python route logs the same warning and
//    lets the API decide. The table trails reality; it does not get a veto.
//  - Thinking Effort below "high" is still a BLOCKER, because the backend still
//    fails fast on it (Runs 5-7: escalation dies or leaks a pre-consult fragment).

import { unquoteYamlScalar } from './configSchema.js';

// Mirror of ADVISOR_COMPATIBILITY in src/Anthropic/client.py. tests/run.ts
// parses client.py and fails if the two drift, so update both together.
export const ADVISOR_COMPATIBILITY: Readonly<Record<string, readonly string[]>> = {
  'claude-opus-5-5': [
    'claude-opus-5-5', 'claude-mythos-5-1', 'claude-fable-5-1',
  ],
  'claude-sonnet-5-5': [
    'claude-opus-5', 'claude-opus-5-5', 'claude-sonnet-5-5',
    'claude-fable-5', 'claude-fable-5-1', 'claude-mythos-5', 'claude-mythos-5-1',
  ],
  'claude-sonnet-5': [
    'claude-mythos-5-1', 'claude-fable-5-1', 'claude-mythos-5', 'claude-fable-5',
    'claude-opus-5', 'claude-opus-4-8', 'claude-opus-4-7', 'claude-sonnet-5', 'claude-opus-5-5',
  ],
  'claude-opus-5': [
    'claude-mythos-5-1', 'claude-fable-5-1', 'claude-mythos-5', 'claude-fable-5',
    'claude-opus-5', 'claude-opus-5-5',
  ],
  'claude-fable-5-1': ['claude-mythos-5-1', 'claude-fable-5-1'],
};

export const PROOFREADING_PATHS = {
  enabled: 'translation.anthropic.advisor.enabled',
  advisor: 'translation.anthropic.advisor.model',
  executor: 'translation.anthropic.model',
  effort: 'translation.anthropic.thinking.effort',
} as const;

type FieldValue = { readonly path: string; readonly rawValue: string };

export type ProofreadingState = {
  readonly enabled: boolean;
  readonly executor: string;
  readonly advisor: string;
  readonly effort: string;
  readonly validAdvisors: readonly string[];
  /** Pairing outside the official table: saved and run anyway, but flagged. */
  readonly warning: string | null;
  /** Why the backend will refuse to start with this config, or null. */
  readonly blocker: string | null;
};

function read(fields: readonly FieldValue[], path: string): string {
  return unquoteYamlScalar(fields.find((field) => field.path === path)?.rawValue ?? '').trim();
}

export function proofreadingState(fields: readonly FieldValue[]): ProofreadingState {
  const executor = read(fields, PROOFREADING_PATHS.executor);
  const advisor = read(fields, PROOFREADING_PATHS.advisor);
  const effort = read(fields, PROOFREADING_PATHS.effort) || 'high';
  const enabled = read(fields, PROOFREADING_PATHS.enabled) === 'true';
  const validAdvisors = ADVISOR_COMPATIBILITY[executor] ?? [];
  let warning: string | null = null;
  if (!validAdvisors.length) warning = `unverified pairing: no compatibility row for executor ${executor || '(unset)'}.`;
  else if (!validAdvisors.includes(advisor)) warning = `unverified pairing: ${advisor || '(unset)'} is not listed for ${executor} (listed: ${validAdvisors.join(', ')}).`;
  const blocker = effort !== 'high' ? `requires Thinking Effort "high" (currently "${effort}").` : null;
  return { enabled, executor, advisor, effort, validAdvisors, warning, blocker };
}

export type ConfigWrite = { readonly path: string; readonly raw: string };
export type ConfigWritePlan =
  | { readonly ok: true; readonly writes: readonly ConfigWrite[]; readonly note: string | null }
  | { readonly ok: false; readonly error: string };

const GATED_PATHS: ReadonlySet<string> = new Set([PROOFREADING_PATHS.executor, PROOFREADING_PATHS.advisor, PROOFREADING_PATHS.effort]);

// Every console save goes through here. Outcomes beyond a plain write:
//  - turning Proofreading Mode ON against a blocker is refused outright;
//  - changing executor/advisor/effort while it is ON into a blocker is allowed,
//    but switches it OFF in the same save. The disable is ordered FIRST so a
//    failed second write leaves the safe state on disk;
//  - any save that leaves the mode ON with an unverified pairing is written
//    as asked, with the warning returned as the note.
export function planConfigWrite(fields: readonly FieldValue[], path: string, raw: string): ConfigWritePlan {
  const write: ConfigWrite = { path, raw };
  const next = fields.map((field) => (field.path === path ? { ...field, rawValue: raw } : field));
  const after = proofreadingState(next);
  const turningOn = path === PROOFREADING_PATHS.enabled && raw === 'true';
  if (turningOn && after.blocker) {
    return { ok: false, error: `Proofreading Mode stays off: ${after.blocker}` };
  }
  if (GATED_PATHS.has(path) && proofreadingState(fields).enabled && after.blocker) {
    return { ok: true, writes: [{ path: PROOFREADING_PATHS.enabled, raw: 'false' }, write], note: `Saved. Proofreading Mode switched off: ${after.blocker}` };
  }
  if ((turningOn || (GATED_PATHS.has(path) && after.enabled)) && after.warning) {
    return { ok: true, writes: [write], note: `Saved with warning — ${after.warning}` };
  }
  return { ok: true, writes: [write], note: null };
}

export function planProofreadingToggle(fields: readonly FieldValue[]): ConfigWritePlan {
  return planConfigWrite(fields, PROOFREADING_PATHS.enabled, proofreadingState(fields).enabled ? 'false' : 'true');
}

// Advisor Model cycles through every choice, listed pairings for the current
// executor first, so the usual pick is a keypress away and an unlisted one is
// still reachable (it saves with a warning, not a refusal).
export function advisorCycleChoices(fields: readonly FieldValue[], allChoices: readonly string[]): readonly string[] {
  const { validAdvisors } = proofreadingState(fields);
  const listed = allChoices.filter((choice) => validAdvisors.includes(choice));
  return [...listed, ...allChoices.filter((choice) => !validAdvisors.includes(choice))];
}
