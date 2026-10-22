import type { MarketplaceStrategy } from './StrategySelection'
import type { MarketRow } from './TokenDetail'

export type StrategyGoalId = 'liquidity-first' | 'swing-trader' | 'squeeze-watch' | 'day-trader' | 'long-hold' | 'unicorn-hunter' | 'custom'

export type StrategyGoal = {
  id: StrategyGoalId
  label: string
  shortLabel: string
  description: string
  entryStyle: string
  draftLabel: string
  identifier: string
  minimumCap: number
  maximumCap: number
  boundary: string
  families: string[]
}

export type GoalFit = {
  id: Exclude<StrategyGoalId, 'custom'>
  score: number
  reason: string
  reportedSimulations: number
  positiveSimulations: number
}

export type MarketPulse = {
  generatedAt: number
  regime: 'Momentum broadening' | 'Market cooling' | 'Mixed market' | 'Thin data'
  tracked: number
  fresh: number
  covered: number
  rising: number
  falling: number
  squeezeSetups: number
  recentLaunches: number
  medianLiquidityUsd: number | null
  medianChangePercent: number | null
  breadthPercent: number | null
  dataQuality: 'Useful local sample' | 'Partial local sample' | 'Insufficient local sample'
  dataNote: string
  recommendedGoal: Exclude<StrategyGoalId, 'custom'>
  fits: GoalFit[]
  movers: { mint: string; symbol: string; changePercent: number; liquidityUsd: number }[]
}

export const STRATEGY_GOALS: StrategyGoal[] = [
  {
    id: 'liquidity-first',
    label: 'Liquidity first',
    shortLabel: 'Defensive',
    description: 'Favor deeper liquidity, smaller entries, and tighter invalidation.',
    entryStyle: 'liquidity',
    draftLabel: 'Liquidity-first research',
    identifier: 'liquidity-first-research',
    minimumCap: 100_000,
    maximumCap: 2_000_000,
    boundary: 'Deeper quoted liquidity can reduce route risk, but does not establish token quality or protect against a rapid liquidity withdrawal.',
    families: ['liquidity', 'post-grad', 'slow_burn'],
  },
  {
    id: 'swing-trader',
    label: 'Swing trading',
    shortLabel: 'Swing',
    description: 'Wait for trend confirmation or a retest, then use staged exits over several days.',
    entryStyle: 'higher-low',
    draftLabel: 'Swing trend research',
    identifier: 'swing-trend-research',
    minimumCap: 50_000,
    maximumCap: 750_000,
    boundary: 'A retained higher-low or retest pattern is historical price evidence, not proof that the trend will continue.',
    families: ['higher-low', 'multi-window', 'retest', 'second-wave', 'recovery'],
  },
  {
    id: 'squeeze-watch',
    label: 'Short-squeeze watch',
    shortLabel: 'Squeeze',
    description: 'Watch compressed price ranges for a breakout with improving liquidity.',
    entryStyle: 'squeeze',
    draftLabel: 'Squeeze breakout watch',
    identifier: 'squeeze-breakout-watch',
    minimumCap: 25_000,
    maximumCap: 250_000,
    boundary: 'Runner has no short-interest or liquidation feed. This is a compression breakout—not proof of a short squeeze.',
    families: ['squeeze'],
  },
  {
    id: 'day-trader',
    label: 'Day trading',
    shortLabel: 'Day trade',
    description: 'Favor fresh momentum, tighter exits, and a four-hour maximum hold.',
    entryStyle: 'momentum',
    draftLabel: 'Day trading momentum',
    identifier: 'day-trading-momentum',
    minimumCap: 10_000,
    maximumCap: 150_000,
    boundary: 'Fast local observations can detect momentum, but they do not guarantee fill quality or continued liquidity.',
    families: ['momentum', 'breakout', 'pullback_reclaim', 'retest', 'second-wave', 'recovery'],
  },
  {
    id: 'long-hold',
    label: 'Longer hold',
    shortLabel: 'Long hold',
    description: 'Require slower, persistent growth and deeper liquidity with a seven-day maximum hold.',
    entryStyle: 'slow_burn',
    draftLabel: 'Longer hold research',
    identifier: 'longer-hold-research',
    minimumCap: 100_000,
    maximumCap: 2_000_000,
    boundary: 'Retained market scans do not establish fundamentals, holder quality, or a genuinely long investment horizon.',
    families: ['slow_burn', 'persistent', 'higher-low', 'multi-window', 'post-grad'],
  },
  {
    id: 'unicorn-hunter',
    label: 'Unicorn hunter',
    shortLabel: 'Unicorn',
    description: 'Research very early tokens showing persistent growth while using staged exits.',
    entryStyle: 'persistent',
    draftLabel: 'Unicorn hunter research',
    identifier: 'unicorn-hunter-research',
    minimumCap: 5_000,
    maximumCap: 75_000,
    boundary: 'This is the most speculative profile. Runner cannot predict a future winner from price and liquidity samples.',
    families: ['persistent', 'liquidity', 'relative', 'breakout'],
  },
  {
    id: 'custom',
    label: 'Custom research',
    shortLabel: 'Custom',
    description: 'Choose an existing supported signal style and set the cap range yourself.',
    entryStyle: 'momentum',
    draftLabel: 'My research strategy',
    identifier: 'my-research-strategy',
    minimumCap: 10_000,
    maximumCap: 25_000,
    boundary: 'Custom settings are not a recommendation and still require monitoring evidence and separate order review.',
    families: [],
  },
]

function finite(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function marketChange(row: MarketRow): number | null {
  const values = (row.market_cap_history || []).map(finite).filter((value): value is number => value !== null && value > 0)
  if (values.length < 2) return null
  return (values[values.length - 1] / values[0] - 1) * 100
}

function median(values: number[]): number | null {
  if (!values.length) return null
  const ordered = values.slice().sort((left, right) => left - right)
  const middle = Math.floor(ordered.length / 2)
  return ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2
}

function squeezeBreakout(row: MarketRow) {
  const values = (row.market_cap_history || []).map(finite).filter((value): value is number => value !== null && value > 0)
  if (values.length < 6) return false
  const base = values.slice(-6, -1)
  const low = Math.min(...base)
  const high = Math.max(...base)
  const latest = values[values.length - 1]
  return low > 0 && high / low - 1 <= .05 && latest > high * 1.02
}

function score(value: number) {
  return Math.max(5, Math.min(95, Math.round(value)))
}

function simulationEvidence(goal: StrategyGoal, simulations: MarketplaceStrategy[]) {
  const reports = simulations.filter((row) => goal.families.includes(row.family) && finite(row.report?.pnl_usd) !== null)
  return {
    reported: reports.length,
    positive: reports.filter((row) => (finite(row.report?.pnl_usd) || 0) > 0).length,
  }
}

export function analyzeMarket(rows: MarketRow[], simulations: MarketplaceStrategy[] = [], now = Date.now() / 1000): MarketPulse {
  const tracked = rows.length
  const freshRows = rows.filter((row) => now >= row.observed_at && now - row.observed_at <= 90)
  const coveredRows = rows.filter((row) => (row.market_cap_history || []).filter((value) => (finite(value) || 0) > 0).length >= 6)
  const changes = coveredRows.map((row) => ({ row, change: marketChange(row) })).filter((item): item is { row: MarketRow; change: number } => item.change !== null)
  const rising = changes.filter((item) => item.change >= 2).length
  const falling = changes.filter((item) => item.change <= -2).length
  const squeezeSetups = freshRows.filter(squeezeBreakout).length
  const recentLaunches = freshRows.filter((row) => Boolean(row.created_at && now >= row.created_at && now - row.created_at <= 3_600)).length
  const liquidRows = freshRows.filter((row) => (finite(row.liquidity_usd) || 0) >= 2_500)
  const deepLiquidity = freshRows.filter((row) => (finite(row.liquidity_usd) || 0) >= 10_000).length
  const breadthPercent = changes.length ? rising / changes.length * 100 : null
  const medianChangePercent = median(changes.map((item) => item.change))
  const medianLiquidityUsd = median(freshRows.map((row) => finite(row.liquidity_usd)).filter((value): value is number => value !== null && value >= 0))
  const coverageRatio = tracked ? coveredRows.length / tracked : 0
  const freshnessRatio = tracked ? freshRows.length / tracked : 0
  const risingRatio = changes.length ? rising / changes.length : 0
  const fallingRatio = changes.length ? falling / changes.length : 0
  const liquidRatio = freshRows.length ? liquidRows.length / freshRows.length : 0
  const deepLiquidityRatio = freshRows.length ? deepLiquidity / freshRows.length : 0
  const recentRatio = freshRows.length ? recentLaunches / freshRows.length : 0

  const regime = tracked < 5 || coveredRows.length < 3
    ? 'Thin data'
    : breadthPercent !== null && breadthPercent >= 60 && (medianChangePercent || 0) > 1
      ? 'Momentum broadening'
      : fallingRatio >= .55 && (medianChangePercent || 0) < -1
        ? 'Market cooling'
        : 'Mixed market'
  const dataQuality = tracked >= 10 && freshRows.length >= 5 && coveredRows.length >= 5
    ? 'Useful local sample'
    : tracked >= 5 && freshRows.length >= 2
      ? 'Partial local sample'
      : 'Insufficient local sample'

  const rawFits: { id: GoalFit['id']; value: number; reason: string }[] = [
    {
      id: 'squeeze-watch',
      value: 12 + freshnessRatio * 18 + coverageRatio * 25 + Math.min(30, squeezeSetups * 10) + Math.max(0, risingRatio - .4) * 25,
      reason: squeezeSetups
        ? `${squeezeSetups} fresh compressed range${squeezeSetups === 1 ? '' : 's'} broke above the retained local band.`
        : 'No fresh compression breakout is visible in the retained sample.',
    },
    {
      id: 'day-trader',
      value: 18 + freshnessRatio * 25 + coverageRatio * 18 + risingRatio * 25 + liquidRatio * 14 - fallingRatio * 10,
      reason: changes.length
        ? `${rising} of ${changes.length} covered tokens are up at least 2% across their retained samples.`
        : 'There is not enough continuous local history to measure short-term breadth.',
    },
    {
      id: 'long-hold',
      value: Math.min(55, 12 + freshnessRatio * 10 + coverageRatio * 12 + deepLiquidityRatio * 18),
      reason: 'Liquidity depth can be screened, but the current market list does not prove long-horizon fundamentals.',
    },
    {
      id: 'unicorn-hunter',
      value: 10 + freshnessRatio * 12 + coverageRatio * 10 + recentRatio * 22 + risingRatio * 18 + liquidRatio * 12,
      reason: recentLaunches
        ? `${recentLaunches} fresh token${recentLaunches === 1 ? '' : 's'} in this sample launched within the last hour.`
        : 'No fresh launch in the current sample has enough retained evidence yet.',
    },
  ]

  const fits = rawFits.map((fit) => {
    const goal = STRATEGY_GOALS.find((item) => item.id === fit.id) as StrategyGoal
    const simulationsForGoal = simulationEvidence(goal, simulations)
    const evidenceCap = dataQuality === 'Useful local sample' ? 95 : dataQuality === 'Partial local sample' ? 70 : 45
    const fitScore = Math.min(evidenceCap, score(fit.value))
    return {
      id: fit.id,
      score: fitScore,
      reason: fit.reason,
      reportedSimulations: simulationsForGoal.reported,
      positiveSimulations: simulationsForGoal.positive,
    }
  }).sort((left, right) => right.score - left.score)

  return {
    generatedAt: now,
    regime,
    tracked,
    fresh: freshRows.length,
    covered: coveredRows.length,
    rising,
    falling,
    squeezeSetups,
    recentLaunches,
    medianLiquidityUsd,
    medianChangePercent,
    breadthPercent,
    dataQuality,
    dataNote: tracked
      ? `${coveredRows.length} of ${tracked} tokens have at least six retained snapshots; this is a bounded watchlist, not the whole market.`
      : 'No retained market rows are available. Refresh the public market before using a strategy suggestion.',
    recommendedGoal: fits[0]?.id || 'day-trader',
    fits,
    movers: changes
      .filter((item) => freshRows.some((row) => row.mint === item.row.mint))
      .sort((left, right) => Math.abs(right.change) - Math.abs(left.change))
      .slice(0, 3)
      .map((item) => ({
        mint: item.row.mint,
        symbol: item.row.symbol,
        changePercent: item.change,
        liquidityUsd: finite(item.row.liquidity_usd) || 0,
      })),
  }
}

export function strategyGoal(id: StrategyGoalId) {
  return STRATEGY_GOALS.find((goal) => goal.id === id) || STRATEGY_GOALS[STRATEGY_GOALS.length - 1]
}
