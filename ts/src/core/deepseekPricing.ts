import { readFileSync } from 'node:fs';
import path from 'node:path';
import { pipelineRoot } from './mtls.js';

type PricingPeriod = 'peak' | 'off_peak';
type Rates = Readonly<{ cache_hit: number; cache_miss: number; output: number }>;
type PricingDocument = Readonly<{
  source: string;
  peak_utc_windows: readonly (readonly [number, number])[];
  // ISO weekdays (Mon=1 .. Sun=7) the peak windows apply on; absent = every day.
  peak_utc_weekdays?: readonly number[];
  peak_note?: string;
  model_display_names: Readonly<Record<string, string>>;
  models: Readonly<Record<string, Readonly<Record<PricingPeriod, Rates>>>>;
}>;

export type DeepSeekPricingStatus = Readonly<{
  period: PricingPeriod;
  label: 'Peak' | 'Off-peak';
  localTime: string;
  utcTime: string;
  modelDisplayNames: Readonly<Record<string, string>>;
  ratesByModel: Readonly<Record<string, Rates>>;
  peakNote: string | null;
  source: string;
}>;

const pricingPath = path.join(pipelineRoot, 'src', 'Deepseek', 'common', 'deepseek_pricing.json');

function loadPricingDocument(): PricingDocument {
  const parsed: unknown = JSON.parse(readFileSync(pricingPath, 'utf8'));
  if (!parsed || typeof parsed !== 'object') throw new Error('DeepSeek pricing data is not an object.');
  const value = parsed as { source?: unknown; peak_utc_windows?: unknown; model_display_names?: unknown; models?: unknown };
  if (typeof value.source !== 'string' || !Array.isArray(value.peak_utc_windows) || !value.model_display_names || typeof value.model_display_names !== 'object' || !value.models || typeof value.models !== 'object') {
    throw new Error('DeepSeek pricing data is incomplete.');
  }
  return value as PricingDocument;
}

const pricing = loadPricingDocument();

function localTime(clock: Date): string {
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short',
  }).format(clock);
}

function utcTime(clock: Date): string {
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC', timeZoneName: 'short',
  }).format(clock);
}

export function deepseekPricingStatusAt(clock: Date = new Date()): DeepSeekPricingStatus {
  // Mirrors deepseek_pricing_status() in src/Deepseek/common/token_telemetry.py.
  const hour = clock.getUTCHours();
  const isoWeekday = clock.getUTCDay() || 7;
  const peakDay = pricing.peak_utc_weekdays ? pricing.peak_utc_weekdays.includes(isoWeekday) : true;
  const period: PricingPeriod = peakDay && pricing.peak_utc_windows.some(([start, end]) => start <= hour && hour < end) ? 'peak' : 'off_peak';
  const ratesByModel = Object.fromEntries(Object.entries(pricing.models).map(([model, periods]) => [model, periods[period]]));
  return {
    period,
    label: period === 'peak' ? 'Peak' : 'Off-peak',
    localTime: localTime(clock),
    utcTime: utcTime(clock),
    modelDisplayNames: pricing.model_display_names,
    ratesByModel,
    peakNote: pricing.peak_note ?? null,
    source: pricing.source,
  };
}
