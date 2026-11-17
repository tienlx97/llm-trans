import type { ConfigFieldState } from './configFile.js';
import { deepseekPricingStatusAt } from './deepseekPricing.js';

type Rates = Readonly<{
  input: number;
  cacheRead: number | null;
  cacheWrite: number | null;
  output: number;
}>;

type Promotion = Readonly<{
  label: string;
  description: string;
  startsAt?: string;
  endsAt?: string;
  rates: Rates;
  explicitCacheRead?: number;
}>;

type ServiceTier = 'flex' | 'priority';

type ModelPricing = Readonly<{
  providerLabel: string;
  displayName: string;
  source: string;
  sourceLabel: string;
  standardRates: Rates;
  explicitCacheRead?: number;
  promotion?: Promotion;
  // Multipliers on every column for a non-standard service tier. Declared only
  // where the provider's table was checked row by row — an absent factor means
  // "unverified", so the estimate stays at Standard rather than guessing.
  serviceTierFactors?: Readonly<Record<ServiceTier, number>>;
  note?: string;
}>;

export type TranslationPricingStatus = Readonly<{
  provider: string;
  model: string;
  providerLabel: string;
  displayName: string;
  rates: Rates | null;
  rateLabel: string;
  condition: string | null;
  cacheReadLabel: string;
  cacheWriteLabel: string;
  source: string | null;
  sourceLabel: string | null;
  note: string | null;
}>;

const PRICING: Readonly<Record<string, ModelPricing>> = {
  'gpt-6-astra': {
    providerLabel: 'OpenAI', displayName: 'GPT-6 Astra', source: 'https://developers.openai.com/api/docs/models/gpt-6-astra', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 10, cacheRead: 1, cacheWrite: 12.5, output: 50 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×. Batch and Flex are 50% of Standard rates.',
  },
  'gpt-6-sol': {
    providerLabel: 'OpenAI', displayName: 'GPT-6 Sol', source: 'https://developers.openai.com/api/docs/models/gpt-6-sol', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 2, cacheRead: 0.2, cacheWrite: 2.5, output: 10 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  'gpt-6-luna': {
    providerLabel: 'OpenAI', displayName: 'GPT-6 Luna', source: 'https://developers.openai.com/api/docs/models/gpt-6-luna', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 0.1, cacheRead: 0.01, cacheWrite: 0.125, output: 0.5 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  // DeepSeek has no row here: its tariff is peak/off-peak clock-sensitive and
  // comes from src/Deepseek/common/deepseek_pricing.json, the same file the
  // Python estimator reads (see deepseekStatus below).
  'qwen3.7-max': {
    providerLabel: 'QwenCloud', displayName: 'Qwen 3.7 Max', source: 'https://www.qwencloud.com/models/qwen3.7-max', sourceLabel: 'QwenCloud model pricing',
    standardRates: { input: 2.5, cacheRead: 0.5, cacheWrite: 3.125, output: 7.5 }, explicitCacheRead: 0.25,
    promotion: {
      label: 'Limited-time 50% off',
      description: 'International Qwen 3.7 Max promotion; provider console remains the authority for eligibility.',
      rates: { input: 1.25, cacheRead: 0.25, cacheWrite: 1.5625, output: 3.75 },
      explicitCacheRead: 0.125,
    },
    note: 'Explicit cache read is $0.125/M during the promotion; implicit cache read is $0.25/M.',
  },
  'qwen3.8-max': {
    providerLabel: 'QwenCloud', displayName: 'Qwen 3.8 Max', source: 'https://www.qwencloud.com/models/qwen3.8-max', sourceLabel: 'QwenCloud model pricing',
    standardRates: { input: 2, cacheRead: 0.25, cacheWrite: 2.5, output: 6 }, explicitCacheRead: 0.17,
    note: 'Explicit cache read is the Qwen 3.8 Max exception ($0.17/M); implicit cache read is $0.25/M.',
  },
  'qwen3.7-plus': {
    providerLabel: 'QwenCloud', displayName: 'Qwen 3.7 Plus', source: 'https://www.qwencloud.com/models/qwen3.7-plus', sourceLabel: 'QwenCloud model pricing',
    standardRates: { input: 0.4, cacheRead: 0.08, cacheWrite: 0.5, output: 1.6 }, explicitCacheRead: 0.04,
    promotion: {
      label: 'Limited-time 20% off', description: 'Current QwenCloud promotional rate; the provider controls its end date.',
      rates: { input: 0.32, cacheRead: 0.064, cacheWrite: 0.4, output: 1.28 },
      explicitCacheRead: 0.032,
    },
    note: 'Explicit cache read is $0.032/M during the promotion; implicit cache read is $0.064/M.',
  },
  'qwen3.7-flash': {
    providerLabel: 'QwenCloud', displayName: 'Qwen 3.7 Flash', source: 'https://www.qwencloud.com/models/qwen3.7-flash', sourceLabel: 'QwenCloud model pricing',
    standardRates: { input: 0.03, cacheRead: 0.006, cacheWrite: 0.038, output: 0.13 }, explicitCacheRead: 0.003,
    note: 'Explicit cache read is $0.003/M; implicit cache read is $0.006/M.',
  },
  'gpt-5.6-sol': {
    providerLabel: 'OpenAI', displayName: 'GPT-5.6 Sol', source: 'https://developers.openai.com/api/docs/models/gpt-5.6-sol', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 4, cacheRead: 0.4, cacheWrite: 5, output: 20 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  'gpt-5.6-terra': {
    providerLabel: 'OpenAI', displayName: 'GPT-5.6 Terra', source: 'https://developers.openai.com/api/docs/models/gpt-5.6-terra', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 2, cacheRead: 0.2, cacheWrite: 2.5, output: 12 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  'gpt-5.6-luna': {
    providerLabel: 'OpenAI', displayName: 'GPT-5.6 Luna', source: 'https://developers.openai.com/api/docs/models/gpt-5.6-luna', sourceLabel: 'OpenAI Docs',
    standardRates: { input: 0.2, cacheRead: 0.02, cacheWrite: 0.25, output: 1.2 },
    note: 'For requests over 272K input tokens: input/cache/write 2×; output 1.5×.',
  },
  'claude-opus-5-5': {
    providerLabel: 'Anthropic', displayName: 'Claude Opus 5.5', source: 'https://platform.claude.com/docs/en/models/opus-5-5/overview', sourceLabel: 'Anthropic Opus 5.5 model page',
    standardRates: { input: 4, cacheRead: 0.2, cacheWrite: 5, output: 20 },
    note: 'Cache read is 5% of input ($0.20/MTok) — half the family 10% multiplier, a per-model rate. Cache write is the 5-minute TTL rate (125% of input); the 1h TTL rate is $8/MTok. Batch: $2 / $10.',
  },
  // The $2/$10 launch price became the standard price: Anthropic's pricing
  // page states the increase scheduled for 2026-09-01 "will not occur". The
  // old card flipped to $3/$15 on that date and over-quoted by 50% since.
  'claude-sonnet-5': {
    providerLabel: 'Anthropic', displayName: 'Claude Sonnet 5', source: 'https://platform.claude.com/docs/en/about-claude/pricing', sourceLabel: 'Anthropic pricing page',
    standardRates: { input: 2, cacheRead: 0.2, cacheWrite: 2.5, output: 10 },
    note: 'Cache read is 10% of input; cache write is the 5-minute TTL rate (125% of input); the 1h TTL rate is $4/MTok.',
  },
  // Mirrors token_telemetry.py's claude-sonnet-5-5 row (cache read $0.10). Anthropic's own
  // migration guide states Sonnet 5.5 "has the same prices as Claude Sonnet 5".
  'claude-sonnet-5-5': {
    providerLabel: 'Anthropic', displayName: 'Claude Sonnet 5.5', source: 'https://platform.claude.com/docs/en/models/sonnet-5-5/overview', sourceLabel: 'Anthropic Sonnet 5.5 model page',
    standardRates: { input: 2, cacheRead: 0.1, cacheWrite: 2.5, output: 10 },
    note: 'Same input/output prices as Claude Sonnet 5; cache read is 5% of input ($0.10/MTok), half the Sonnet 5 rate. Cache write is the 5-minute TTL rate (125% of input); the 1h TTL rate is $4/MTok.',
  },
  'claude-opus-5': {
    providerLabel: 'Anthropic', displayName: 'Claude Opus 5', source: 'https://www.anthropic.com/news/claude-opus-5', sourceLabel: 'Anthropic Opus 5 announcement',
    standardRates: { input: 5, cacheRead: 0.5, cacheWrite: 6.25, output: 25 },
    note: 'Cache read is 10% of input; cache write is the 5-minute TTL rate (125% of input); the 1h TTL rate is $10/MTok.',
  },
  // Mirrors token_telemetry.py's gemini rows. No cache-write token class:
  // explicit caches bill storage per hour instead, which is not a per-token rate.
  'gemini-3.8-flash': {
    providerLabel: 'Google Gemini', displayName: 'Gemini 3.8 Flash', source: 'https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash', sourceLabel: 'Gemini API pricing',
    standardRates: { input: 1.5, cacheRead: 0.15, cacheWrite: null, output: 7.5 },
    promotion: {
      label: 'Introductory pricing', endsAt: '2027-01-01T00:00:00Z',
      description: 'Introductory rates through 31 December 2026; standard pricing takes effect 1 January 2027.',
      rates: { input: 0.75, cacheRead: 0.075, cacheWrite: null, output: 3.75 },
    },
    note: 'Output includes thinking tokens. Explicit-cache storage: $0.50/MTok/hour (intro), $1.00 from 2027.',
  },
  'gemini-3.6-flash': {
    providerLabel: 'Google Gemini', displayName: 'Gemini 3.6 Flash', source: 'https://ai.google.dev/gemini-api/docs/pricing#gemini-3.6-flash', sourceLabel: 'Gemini API pricing',
    standardRates: { input: 1.5, cacheRead: 0.15, cacheWrite: null, output: 7.5 },
    promotion: {
      label: 'Introductory pricing', endsAt: '2027-01-01T00:00:00Z',
      description: 'Introductory rates through 31 December 2026; standard pricing takes effect 1 January 2027.',
      rates: { input: 0.75, cacheRead: 0.075, cacheWrite: null, output: 3.75 },
    },
    note: 'Output includes thinking tokens. Explicit-cache storage: $0.50/MTok/hour (intro), $1.00 from 2027.',
  },
  // Mirrors token_telemetry.py's gemini-4-argon / _GEMINI_ARGON_INTRO_RATES.
  // Verified 2026-10-14 against the pricing page: every Batch/Flex column is
  // exactly half and every Priority column exactly 1.8×, intro and standard alike.
  'gemini-4-argon': {
    providerLabel: 'Google Gemini', displayName: 'Gemini 4 Argon', source: 'https://ai.google.dev/gemini-api/docs/pricing#gemini-4-argon', sourceLabel: 'Gemini API pricing',
    standardRates: { input: 4, cacheRead: 0.2, cacheWrite: null, output: 20 },
    promotion: {
      label: 'Introductory pricing', endsAt: '2026-12-01T00:00:00Z',
      description: 'Introductory rates through 30 November 2026; standard pricing takes effect 1 December 2026.',
      rates: { input: 2, cacheRead: 0.1, cacheWrite: null, output: 10 },
    },
    serviceTierFactors: { flex: 0.5, priority: 1.8 },
    note: 'Output includes thinking tokens. Implicit caching only — cache reads bill at 5% of input (95% off) on shared prefixes of 6,144+ tokens; durable caching on by default (per Google’s Gemini Enterprise Argon guide); no explicit cache, no storage charge. No >200K tier: the whole 1,048,576-token window bills flat. Not on the Free tier. Search/Maps grounding: 5,000 free requests/month (shared across Gemini 3+), then $14 per 1,000.',
  },
  'gemini-3.1-pro-preview': {
    providerLabel: 'Google Gemini', displayName: 'Gemini 3.1 Pro Preview', source: 'https://ai.google.dev/gemini-api/docs/pricing#gemini-3.1-pro-preview', sourceLabel: 'Gemini API pricing',
    standardRates: { input: 2, cacheRead: 0.2, cacheWrite: null, output: 12 },
    note: 'Prompts over 200K tokens bill the whole request at $4 / $0.40 / $18. Batch halves input/output; cache reads stay at Standard. Storage $4.50/MTok/hour.',
  },
  'claude-fable-5-1': {
    providerLabel: 'Anthropic', displayName: 'Claude Fable 5.1', source: 'https://platform.claude.com/docs/en/models/fable-5-1/overview', sourceLabel: 'Anthropic Fable 5.1 model page',
    standardRates: { input: 10, cacheRead: 0.25, cacheWrite: 12.5, output: 50 },
    note: 'Cache read is 2.5% of input — a per-model rate Fable 5.1 states outright, NOT the family 10% multiplier the other two follow. Cache write is the 5-minute TTL rate (125% of input); the 1h TTL rate is $20/MTok.',
  },
};

function configuredValue(fields: readonly ConfigFieldState[], path: string): string {
  return fields.find((field) => field.path === path)?.rawValue.trim() ?? '';
}

function currentPromotion(pricing: ModelPricing, clock: Date): Promotion | undefined {
  const promotion = pricing.promotion;
  if (!promotion) return undefined;
  const now = clock.getTime();
  const start = promotion.startsAt ? Date.parse(promotion.startsAt) : Number.NEGATIVE_INFINITY;
  const end = promotion.endsAt ? Date.parse(promotion.endsAt) : Number.POSITIVE_INFINITY;
  return now >= start && now < end ? promotion : undefined;
}

function deepseekStatus(model: string, clock: Date): TranslationPricingStatus {
  const tariff = deepseekPricingStatusAt(clock);
  // Same fallback as token_telemetry._rates_for_model: any *flash* id bills at
  // Flash, everything else at V4 Pro.
  const key = tariff.ratesByModel[model] ? model : model.includes('flash') ? 'deepseek-flash' : 'deepseek-v4-pro';
  const rates = tariff.ratesByModel[key];
  return {
    provider: 'deepseek', model, providerLabel: 'DeepSeek', displayName: tariff.modelDisplayNames[key] ?? (model || 'No configured model'),
    rates: rates ? { input: rates.cache_miss, cacheRead: rates.cache_hit, cacheWrite: null, output: rates.output } : null,
    rateLabel: `${tariff.label} API pricing · ${tariff.utcTime}`,
    condition: tariff.peakNote,
    cacheReadLabel: 'cache read', cacheWriteLabel: 'cache write',
    source: tariff.source, sourceLabel: 'DeepSeek API pricing docs',
    note: 'Off-peak rates are half of peak. Automatic prefix cache: no cache-write charge.',
  };
}

export function translationPricingStatus(fields: readonly ConfigFieldState[], clock: Date = new Date()): TranslationPricingStatus {
  const provider = configuredValue(fields, 'translation.provider').toLowerCase();
  const model = configuredValue(fields, `translation.${provider}.model`).toLowerCase();
  if (provider === 'deepseek') return deepseekStatus(model, clock);
  const pricing = PRICING[model];
  if (!pricing) {
    const labels: Readonly<Record<string, string>> = { qwen: 'Qwen / Model Studio', deepseek: 'DeepSeek', openai: 'OpenAI', anthropic: 'Anthropic', gemini: 'Google Gemini' };
    return {
      provider, model, providerLabel: labels[provider] ?? (provider || 'Unknown provider'), displayName: model || 'No configured model', rates: null,
      rateLabel: 'Official API rates unavailable', condition: null, source: null, sourceLabel: null,
      cacheReadLabel: 'cache read',
      cacheWriteLabel: 'cache write',
      note: 'Add a first-party pricing entry before estimating this route.',
    };
  }
  const promotion = currentPromotion(pricing, clock);
  const explicitQwenCache = provider === 'qwen' && configuredValue(fields, 'translation.qwen.caching.explicit') === 'true';
  const batch = (provider === 'openai' || provider === 'anthropic' || provider === 'gemini') && configuredValue(fields, `translation.${provider}.batch.enabled`) === 'true';
  const baseRates = promotion?.rates ?? pricing.standardRates;
  // Gemini 3.1 Pro's Batch table lists context caching "Same as Standard" —
  // the one documented exception to the half-rate rule (token_telemetry.py agrees).
  const batchCacheFactor = model.startsWith('gemini-3.1-pro') ? 1 : 0.5;
  // Batch is its own rate card, so it wins outright; a service tier never
  // stacks on top of it.
  const tierSetting = provider === 'gemini' ? configuredValue(fields, 'translation.gemini.service_tier').toLowerCase() : '';
  const tierFactors = pricing.serviceTierFactors;
  const tier: ServiceTier | null = !batch && tierFactors && (tierSetting === 'flex' || tierSetting === 'priority') ? tierSetting : null;
  const tierFactor = tier && tierFactors ? tierFactors[tier] : 1;
  const scale = (value: number | null, factor: number): number | null => (value === null ? null : value * factor);
  const rates = batch ? {
    input: baseRates.input * 0.5,
    cacheRead: scale(baseRates.cacheRead, batchCacheFactor),
    cacheWrite: scale(baseRates.cacheWrite, 0.5),
    output: baseRates.output * 0.5,
  } : tier ? {
    input: baseRates.input * tierFactor,
    cacheRead: scale(baseRates.cacheRead, tierFactor),
    cacheWrite: scale(baseRates.cacheWrite, tierFactor),
    output: baseRates.output * tierFactor,
  } : baseRates;
  const tierLabel = tier === 'priority' ? 'Priority pricing' : tier === 'flex' ? 'Flex pricing' : null;
  const explicitCacheRead = promotion?.explicitCacheRead ?? pricing.explicitCacheRead;
  return {
    provider, model, providerLabel: pricing.providerLabel, displayName: pricing.displayName,
    rates: explicitQwenCache && explicitCacheRead !== undefined ? { ...rates, cacheRead: explicitCacheRead } : rates,
    rateLabel: batch ? 'Batch API pricing' : tierLabel ?? promotion?.label ?? 'Standard API pricing',
    condition: promotion?.description ?? null,
    cacheReadLabel: explicitQwenCache ? 'explicit cache read' : provider === 'qwen' ? 'implicit cache read' : 'cache read',
    cacheWriteLabel: 'cache write',
    source: pricing.source, sourceLabel: pricing.sourceLabel, note: pricing.note ?? null,
  };
}

export function usdPerMillion(value: number | null): string {
  return value === null ? '—' : `$${value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')}`;
}
